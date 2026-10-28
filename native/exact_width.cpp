// Exact half-strip width decider (the research repo's cpp/hsw.cpp, DFA mode with streamed minimization),
// optionally with a checkpoint after every row so a crash or a stop loses at most one row (without
// --checkpoint nothing is saved and every run starts from the beginning).
//
// Row r's language is the set of frontiers (6 rows of occupancy per column) left by fillings of the first
// r rows of the width-W half-strip, as a minimal acyclic DFA. The run ends when the language is empty (no
// rectangle has a side W), accepts the flat frontier (a W x r rectangle tiles), or repeats an earlier row
// exactly (the half-strip fills forever, so this run can never end in a death).
//
// Output on stdout, one line each, flushed:
//   start <row>                                     first row of this run (resumed or fresh)
//   row <r> states <n> words <x> seconds <s>        row r's language; seconds count every run so far
//   counts <n_start> <n_start+1> ...                state counts of every row, just before the verdict
//   dead <r> | rectangle <r> | forever <earlier> <r>
// Errors go to stderr with exit code 2.

#include <algorithm>
#include <array>
#include <chrono>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <filesystem>
#include <functional>
#include <iostream>
#include <map>
#include <memory>
#include <set>
#include <sstream>
#include <string>
#include <tuple>
#include <unordered_map>
#include <vector>

#ifdef _WIN32
#define FSEEK _fseeki64
#define FTELL _ftelli64
#else
#define FSEEK fseeko
#define FTELL ftello
#endif

namespace {

using Cell = std::pair<int, int>;  // (row, col)
constexpr int SYMBOLS = 64;        // 6 rows of occupancy per column
constexpr int LEFT_REACH = 3;      // a piece anchored at column c touches columns >= c-3
constexpr int RIGHT_REACH = 6;     // ... and <= c+6
constexpr int COL_BITS = 7;        // rows r..r+6 inside the transducer
constexpr int MAX_ROWS = COL_BITS;
constexpr int32_t FLUSH_BASE = -2;  // NFA state q = -2 - k: the end flush with k columns emitted
constexpr char CHECKPOINT_MAGIC[8] = {'F', 'E', '7', '0', 'X', 'W', '0', '1'};
constexpr char CHECKPOINT_END[4] = {'E', 'N', 'D', '!'};
constexpr size_t IO_CHUNK = 1 << 26;
constexpr int DEFAULT_START_ROW = 8;

[[noreturn]] void fail(const std::string& message) {
    std::cerr << "error: " << message << "\n" << std::flush;
    std::exit(2);
}

struct Orientation {
    std::vector<Cell> cells;  // (dr >= 0, dc), relative to the first row-major cell
    int minDc = 0;
};

std::vector<Cell> normalize(std::vector<Cell> c) {
    int mr = 1 << 30, mc = 1 << 30;
    for (auto& p : c) { mr = std::min(mr, p.first); mc = std::min(mc, p.second); }
    for (auto& p : c) { p.first -= mr; p.second -= mc; }
    std::sort(c.begin(), c.end());
    return c;
}

std::vector<Orientation> orientationsOf(const std::vector<Cell>& base) {
    std::set<std::vector<Cell>> found;
    auto cur = normalize(base);
    for (int f = 0; f < 2; f++) {
        for (int r = 0; r < 4; r++) {
            found.insert(cur);
            std::vector<Cell> turned;
            for (auto& p : cur) turned.push_back({p.second, -p.first});
            cur = normalize(turned);
        }
        std::vector<Cell> flipped;
        for (auto& p : cur) flipped.push_back({p.first, -p.second});
        cur = normalize(flipped);
    }
    std::vector<Orientation> out;
    for (auto& o : found) {
        Orientation ori;
        for (auto& c : o) {
            int dr = c.first - o[0].first, dc = c.second - o[0].second;
            if (dr >= MAX_ROWS || dc < -LEFT_REACH || dc > RIGHT_REACH)
                fail("the piece must fit in 7 rows and 3 columns left / 6 right of its first cell");
            ori.cells.push_back({dr, dc});
            ori.minDc = std::min(ori.minDc, dc);
        }
        out.push_back(ori);
    }
    return out;
}

// Dense DFA over frontier columns, for the width-independent first rows.
struct Dfa {
    std::vector<std::array<int32_t, SYMBOLS>> next;  // -1 = dead
    std::vector<char> accept;
    int32_t start = 0;
    size_t size() const { return next.size(); }
};

// Transducer state (one input column consumed per step).
// win: columns c-3..c-1 (finalized except output), 7 bits each; pend: columns c..c+6 from placed pieces
struct NState {
    int32_t q;
    uint8_t cap;  // min(columns processed, 3)
    uint32_t win;
    uint64_t pend;
    bool operator==(const NState& o) const { return q == o.q && cap == o.cap && win == o.win && pend == o.pend; }
};
struct NStateHash {
    size_t operator()(const NState& s) const {
        uint64_t h = (uint64_t)(uint32_t)s.q * 0x9E3779B97F4A7C15ULL ^ ((uint64_t)s.win << 3 | s.cap);
        h ^= s.pend + 0x9E3779B97F4A7C15ULL + (h << 6) + (h >> 2);
        h *= 0xBF58476D1CE4E5B9ULL;
        h ^= h >> 31;
        return h;
    }
};

inline uint32_t colOf(uint64_t packed, int i) { return (packed >> (i * COL_BITS)) & 0x7F; }

// Every way to consume input column x in state s: (output symbol or -1, next state). `nq` is the input
// automaton's state after x.
void stepColumn(const std::vector<Orientation>& oris, const NState& s, int x, int32_t nq,
                std::vector<std::pair<int, NState>>& out) {
    uint32_t p0 = colOf(s.pend, 0);
    if (x & p0) return;
    uint32_t cols[10];
    for (int i = 0; i < 3; i++) cols[i] = (s.win >> (i * COL_BITS)) & 0x7F;
    cols[3] = x | p0;
    for (int i = 1; i <= 6; i++) cols[3 + i] = colOf(s.pend, i);
    auto emit = [&](const uint32_t* c) {
        NState n;
        n.q = nq;
        n.cap = (uint8_t)std::min(s.cap + 1, LEFT_REACH);
        n.win = c[1] | c[2] << COL_BITS | c[3] << (2 * COL_BITS);
        n.pend = 0;
        for (int i = 0; i < 6; i++) n.pend |= (uint64_t)c[4 + i] << (i * COL_BITS);
        out.push_back({s.cap == LEFT_REACH ? (int)(c[0] >> 1) : -1, n});
    };
    if (cols[3] & 1) { emit(cols); return; }
    for (auto& o : oris) {
        if (o.minDc < -(int)s.cap) continue;  // would cross the left wall
        bool ok = true;
        for (auto& c : o.cells) if (cols[3 + c.second] >> c.first & 1) { ok = false; break; }
        if (!ok) continue;
        uint32_t nc[10];
        memcpy(nc, cols, sizeof nc);
        for (auto& c : o.cells) nc[3 + c.second] |= 1u << c.first;
        emit(nc);
    }
}

// The end flush: after the last column, emit the three window columns, then accept.
void flushStep(const NState& s, bool inputAccepts, std::vector<std::pair<int, NState>>& out) {
    if (s.q >= 0) {
        if (inputAccepts && s.pend == 0 && s.cap == LEFT_REACH) {
            NState f = s;
            f.q = FLUSH_BASE - 1;
            out.push_back({(int)((s.win & 0x7F) >> 1), f});
        }
        return;
    }
    int k = FLUSH_BASE - s.q;
    if (k < 3) {
        NState f = s;
        f.q = FLUSH_BASE - (k + 1);
        out.push_back({(int)(((s.win >> (k * COL_BITS)) & 0x7F) >> 1), f});
    }
}

// The next row's language by subset construction over the transducer (outputs as the alphabet).
Dfa nextLanguageDense(const std::vector<Orientation>& oris, const Dfa& in) {
    std::unordered_map<NState, int32_t, NStateHash> nid;
    std::vector<NState> nstates;
    auto internN = [&](const NState& s) -> int32_t {
        auto it = nid.find(s);
        if (it != nid.end()) return it->second;
        int32_t id = (int32_t)nstates.size();
        nid.emplace(s, id);
        nstates.push_back(s);
        return id;
    };
    std::vector<std::vector<std::pair<int8_t, int32_t>>> ntrans;
    std::vector<char> ntransDone;
    std::vector<std::pair<int, NState>> buf;
    auto transitionsOf = [&](int32_t id) -> const std::vector<std::pair<int8_t, int32_t>>& {
        if ((int32_t)ntrans.size() <= id) { ntrans.resize(nstates.size() * 2 + 16); ntransDone.resize(nstates.size() * 2 + 16, 0); }
        if (ntransDone[id]) return ntrans[id];
        NState s = nstates[id];
        buf.clear();
        if (s.q >= 0)
            for (int x = 0; x < SYMBOLS; x++) { int32_t nq = in.next[s.q][x]; if (nq >= 0) stepColumn(oris, s, x, nq, buf); }
        flushStep(s, s.q >= 0 && in.accept[s.q], buf);
        std::vector<std::pair<int8_t, int32_t>> res;
        for (auto& [sym, n] : buf) res.push_back({(int8_t)sym, internN(n)});
        if ((int32_t)ntrans.size() <= id) { ntrans.resize(nstates.size() * 2 + 16); ntransDone.resize(nstates.size() * 2 + 16, 0); }
        ntrans[id] = std::move(res);
        ntransDone[id] = 1;
        return ntrans[id];
    };
    auto isAccept = [&](int32_t id) { return nstates[id].q == FLUSH_BASE - 3; };

    std::vector<int32_t> cur{internN(NState{in.start, 0, 0, 0})};
    for (int k = 0; k < LEFT_REACH; k++) {
        std::set<int32_t> nxt;
        for (int32_t id : cur) for (auto& [sym, t] : transitionsOf(id)) if (sym == -1) nxt.insert(t);
        cur.assign(nxt.begin(), nxt.end());
    }

    std::map<std::vector<int32_t>, int32_t> did;
    std::vector<std::vector<int32_t>> sets;
    Dfa out;
    auto internD = [&](const std::vector<int32_t>& set) -> int32_t {
        auto it = did.find(set);
        if (it != did.end()) return it->second;
        int32_t id = (int32_t)sets.size();
        did.emplace(set, id);
        sets.push_back(set);
        std::array<int32_t, SYMBOLS> row;
        row.fill(-1);
        out.next.push_back(row);
        out.accept.push_back(0);
        return id;
    };
    if (cur.empty()) { internD({}); return out; }
    internD(cur);
    for (size_t d = 0; d < sets.size(); d++) {
        std::vector<int32_t> set = sets[d];
        bool acc = false;
        std::array<std::vector<int32_t>, SYMBOLS> succ;
        for (int32_t id : set) {
            if (isAccept(id)) acc = true;
            for (auto& [sym, t] : transitionsOf(id)) if (sym >= 0) succ[sym].push_back(t);
        }
        out.accept[d] = acc;
        for (int a = 0; a < SYMBOLS; a++) {
            if (succ[a].empty()) continue;
            std::sort(succ[a].begin(), succ[a].end());
            succ[a].erase(std::unique(succ[a].begin(), succ[a].end()), succ[a].end());
            out.next[d][a] = internD(succ[a]);
        }
    }
    return out;
}

// Trim to reachable and co-reachable states, then Moore refinement.
Dfa minimizeDense(const Dfa& d) {
    size_t n = d.size();
    std::vector<std::vector<int32_t>> pred(n);
    for (size_t i = 0; i < n; i++) for (int a = 0; a < SYMBOLS; a++) if (d.next[i][a] >= 0) pred[d.next[i][a]].push_back((int32_t)i);
    std::vector<char> co(n, 0), reach(n, 0);
    std::vector<int32_t> st;
    for (size_t i = 0; i < n; i++) if (d.accept[i]) { co[i] = 1; st.push_back((int32_t)i); }
    while (!st.empty()) { int32_t v = st.back(); st.pop_back(); for (int32_t p : pred[v]) if (!co[p]) { co[p] = 1; st.push_back(p); } }
    if (co[d.start]) { reach[d.start] = 1; st.push_back(d.start); }
    while (!st.empty()) {
        int32_t v = st.back();
        st.pop_back();
        for (int a = 0; a < SYMBOLS; a++) { int32_t t = d.next[v][a]; if (t >= 0 && co[t] && !reach[t]) { reach[t] = 1; st.push_back(t); } }
    }
    std::vector<int32_t> alive;
    for (size_t i = 0; i < n; i++) if (reach[i]) alive.push_back((int32_t)i);
    Dfa out;
    std::array<int32_t, SYMBOLS> empty;
    empty.fill(-1);
    if (alive.empty()) { out.next.push_back(empty); out.accept.push_back(0); return out; }
    std::vector<int32_t> cls(n, -1);
    for (int32_t v : alive) cls[v] = d.accept[v] ? 1 : 0;
    size_t numCls = 0;
    while (true) {
        std::map<std::vector<int32_t>, int32_t> sig;
        std::vector<int32_t> ncls(n, -1);
        for (int32_t v : alive) {
            std::vector<int32_t> key;
            key.reserve(SYMBOLS + 1);
            key.push_back(cls[v]);
            for (int a = 0; a < SYMBOLS; a++) { int32_t t = d.next[v][a]; key.push_back(t >= 0 && reach[t] ? cls[t] : -1); }
            auto it = sig.find(key);
            if (it == sig.end()) it = sig.emplace(key, (int32_t)sig.size()).first;
            ncls[v] = it->second;
        }
        cls = ncls;
        if (sig.size() == numCls) break;
        numCls = sig.size();
    }
    out.next.assign(numCls, empty);
    out.accept.assign(numCls, 0);
    for (int32_t v : alive) {
        int32_t c = cls[v];
        out.accept[c] = d.accept[v];
        for (int a = 0; a < SYMBOLS; a++) { int32_t t = d.next[v][a]; if (t >= 0 && reach[t]) out.next[c][a] = cls[t]; }
    }
    out.start = cls[d.start];
    return out;
}

// Sparse layered DFA: edges of state v are off[v]..off[v+1], in increasing symbol order.
struct SDfa {
    std::vector<uint64_t> off{0};
    std::vector<uint8_t> sym;
    std::vector<int32_t> tgt;
    std::vector<uint8_t> acc;
    int32_t start = 0;
    size_t size() const { return acc.size(); }
    int32_t next(int32_t v, int a) const {
        for (uint64_t e = off[v]; e < off[v + 1]; e++) if (sym[e] == a) return tgt[e];
        return -1;
    }
    void release() { *this = SDfa(); off.clear(); }
    static SDfa emptyLanguage() { SDfa d; d.acc.push_back(0); d.off.push_back(0); return d; }
};

// Open-addressing table of int32 ids; key equality and hashing supplied by the caller.
class IdTable {
public:
    IdTable() { slots.assign(1 << 16, -1); }
    template <class Eq, class Make>
    int32_t findOrInsert(uint64_t h, Eq eq, Make make, const std::function<uint64_t(int32_t)>& rehashOf) {
        if ((count + 1) * 2 > slots.size()) grow(rehashOf);
        size_t mask = slots.size() - 1, i = h & mask;
        while (slots[i] >= 0) {
            if (eq(slots[i])) return slots[i];
            i = (i + 1) & mask;
        }
        int32_t id = make();
        slots[i] = id;
        count++;
        return id;
    }

private:
    std::vector<int32_t> slots;
    size_t count = 0;
    void grow(const std::function<uint64_t(int32_t)>& rehashOf) {
        std::vector<int32_t> old;
        old.swap(slots);
        slots.assign(old.size() * 2, -1);
        size_t mask = slots.size() - 1;
        for (int32_t id : old) {
            if (id < 0) continue;
            size_t i = rehashOf(id) & mask;
            while (slots[i] >= 0) i = (i + 1) & mask;
            slots[i] = id;
        }
    }
};

uint64_t hashInts(const int32_t* p, size_t n) {
    uint64_t h = 0x9E3779B97F4A7C15ULL ^ n;
    for (size_t i = 0; i < n; i++) { h ^= (uint64_t)(uint32_t)p[i] + 0x9E3779B97F4A7C15ULL + (h << 6) + (h >> 2); h *= 0xBF58476D1CE4E5B9ULL; }
    return h ^ (h >> 29);
}

uint64_t mixEdge(uint64_t h, uint8_t sym, int32_t cls) {
    h ^= ((uint64_t)sym << 32 | (uint32_t)cls) * 0x9E3779B97F4A7C15ULL;
    h *= 0xBF58476D1CE4E5B9ULL;
    return h ^ (h >> 29);
}

constexpr uint64_t HASH_ACCEPT = 0x51ED270B27EB0A5FULL;
constexpr uint64_t HASH_REJECT = 0x2545F4914F6CDD1DULL;

// canAcceptIn[k][q]: some word of length exactly k leads q to accept
std::vector<std::vector<char>> acceptLengths(const Dfa& lang, int maxLen) {
    std::vector<std::vector<char>> can(maxLen + 1, std::vector<char>(lang.size(), 0));
    for (size_t q = 0; q < lang.size(); q++) can[0][q] = lang.accept[q];
    for (int k = 1; k <= maxLen; k++)
        for (size_t q = 0; q < lang.size(); q++)
            for (int a = 0; a < SYMBOLS; a++) { int32_t t = lang.next[q][a]; if (t >= 0 && can[k - 1][t]) { can[k][q] = 1; break; } }
    return can;
}

// The width-W words of a dense language, as a layered DFA.
SDfa restrictLength(const Dfa& d, int W) {
    auto can = acceptLengths(d, W);
    if (!can[W][d.start]) return SDfa::emptyLanguage();
    SDfa out;
    std::unordered_map<uint64_t, int32_t> id;
    std::vector<std::pair<int32_t, int>> st;
    auto intern = [&](int32_t q, int pos) {
        uint64_t key = (uint64_t)(uint32_t)q << 16 | (uint64_t)pos;
        auto it = id.find(key);
        if (it != id.end()) return it->second;
        int32_t v = (int32_t)st.size();
        id.emplace(key, v);
        st.push_back({q, pos});
        out.acc.push_back(pos == W && d.accept[q]);
        return v;
    };
    intern(d.start, 0);
    for (size_t v = 0; v < st.size(); v++) {
        auto [q, pos] = st[v];
        if (pos < W)
            for (int a = 0; a < SYMBOLS; a++) {
                int32_t t = d.next[q][a];
                if (t < 0 || !can[W - pos - 1][t]) continue;
                int32_t nv = intern(t, pos + 1);
                out.sym.push_back((uint8_t)a);
                out.tgt.push_back(nv);
            }
        out.off.push_back(out.sym.size());
    }
    return out;
}

SDfa minimizeAcyclic(const SDfa& d) {
    size_t n = d.size();
    std::vector<int32_t> order{d.start};
    std::vector<char> seen(n, 0);
    seen[d.start] = 1;
    for (size_t i = 0; i < order.size(); i++)
        for (uint64_t e = d.off[order[i]]; e < d.off[order[i] + 1]; e++) if (!seen[d.tgt[e]]) { seen[d.tgt[e]] = 1; order.push_back(d.tgt[e]); }
    std::vector<char>().swap(seen);
    std::vector<int32_t> cls(n, -1), rep;
    std::vector<uint64_t> repHash;
    IdTable table;
    std::function<uint64_t(int32_t)> rh = [&](int32_t c) { return repHash[c]; };
    for (size_t i = order.size(); i-- > 0;) {
        int32_t v = order[i];
        bool useful = d.acc[v];
        uint64_t h = d.acc[v] ? HASH_ACCEPT : HASH_REJECT;
        for (uint64_t e = d.off[v]; e < d.off[v + 1]; e++) {
            int32_t c = cls[d.tgt[e]];
            if (c < 0) continue;
            useful = true;
            h = mixEdge(h, d.sym[e], c);
        }
        if (!useful) continue;
        auto sameAs = [&](int32_t c) {
            int32_t r = rep[c];
            if (d.acc[r] != d.acc[v]) return false;
            uint64_t a = d.off[r], ae = d.off[r + 1], b = d.off[v], be = d.off[v + 1];
            while (true) {
                while (a < ae && cls[d.tgt[a]] < 0) a++;
                while (b < be && cls[d.tgt[b]] < 0) b++;
                if (a == ae || b == be) return a == ae && b == be;
                if (d.sym[a] != d.sym[b] || cls[d.tgt[a]] != cls[d.tgt[b]]) return false;
                a++;
                b++;
            }
        };
        cls[v] = table.findOrInsert(h, sameAs, [&]() { rep.push_back(v); repHash.push_back(h); return (int32_t)rep.size() - 1; }, rh);
    }
    if (cls[d.start] < 0) return SDfa::emptyLanguage();
    SDfa out;
    out.acc.resize(rep.size());
    for (size_t c = 0; c < rep.size(); c++) {
        int32_t r = rep[c];
        out.acc[c] = d.acc[r];
        for (uint64_t e = d.off[r]; e < d.off[r + 1]; e++) {
            int32_t t = cls[d.tgt[e]];
            if (t < 0) continue;
            out.sym.push_back(d.sym[e]);
            out.tgt.push_back(t);
        }
        out.off.push_back(out.sym.size());
    }
    out.start = cls[d.start];
    return out;
}

void writeAll(FILE* f, const void* data, size_t bytes, const char* what) {
    const char* p = static_cast<const char*>(data);
    while (bytes) {
        size_t chunk = std::min(bytes, IO_CHUNK);
        if (fwrite(p, 1, chunk, f) != chunk) fail(std::string("write failed (") + what + "); is the disk full?");
        p += chunk;
        bytes -= chunk;
    }
}

void readAll(FILE* f, void* data, size_t bytes, const char* what) {
    char* p = static_cast<char*>(data);
    while (bytes) {
        size_t chunk = std::min(bytes, IO_CHUNK);
        if (fread(p, 1, chunk, f) != chunk) fail(std::string("read failed (") + what + ")");
        p += chunk;
        bytes -= chunk;
    }
}

template <class T>
void writeValue(FILE* f, const T& v, const char* what) { writeAll(f, &v, sizeof v, what); }
template <class T>
T readValue(FILE* f, const char* what) { T v; readAll(f, &v, sizeof v, what); return v; }

// Writes each output layer of the transducer to a scratch file: n, e, acc[n], off[n+1] (uint32), sym[e],
// tgt[e] (local ids in the next layer). Memory stays at one layer.
struct DiskSink {
    FILE* f = nullptr;
    bool isEmpty = false;
    std::vector<int64_t> layerPos;
    std::vector<uint8_t> acc, sym;
    std::vector<uint32_t> off;
    std::vector<int32_t> tgt;
    void empty() { isEmpty = true; }
    void beginLayer() { acc.clear(); sym.clear(); tgt.clear(); off.assign(1, 0); }
    void state(bool a, const uint8_t* s, const int32_t* t, int k) {
        acc.push_back(a);
        sym.insert(sym.end(), s, s + k);
        tgt.insert(tgt.end(), t, t + k);
        if (sym.size() > UINT32_MAX) fail("a layer has too many edges for 32-bit offsets");
        off.push_back((uint32_t)sym.size());
    }
    void endLayer() {
        layerPos.push_back(FTELL(f));
        uint64_t n = acc.size(), e = sym.size();
        writeValue(f, n, "scratch");
        writeValue(f, e, "scratch");
        writeAll(f, acc.data(), n, "scratch");
        writeAll(f, off.data(), 4 * (n + 1), "scratch");
        writeAll(f, sym.data(), e, "scratch");
        writeAll(f, tgt.data(), 4 * e, "scratch");
    }
};

// One row of the transducer over a layered DFA, layer by layer into the sink.
void transduceLayered(const std::vector<Orientation>& oris, const SDfa& in, DiskSink& sink) {
    NStateHash nh;
    std::vector<std::pair<int, NState>> buf;
    auto expand = [&](const NState& s) {
        buf.clear();
        if (s.q >= 0)
            for (uint64_t e = in.off[s.q]; e < in.off[s.q + 1]; e++) stepColumn(oris, s, in.sym[e], in.tgt[e], buf);
        flushStep(s, s.q >= 0 && in.acc[s.q], buf);
    };
    auto isAccept = [&](const NState& s) { return s.q == FLUSH_BASE - 3; };

    struct Layer {
        std::vector<NState> nstates;
        IdTable nid;
        std::vector<int32_t> setPool;
        std::vector<uint64_t> setStart{0};
        std::vector<uint64_t> setHash;
        IdTable did;
    };
    auto internN = [&](Layer& L, const NState& s) -> int32_t {
        std::function<uint64_t(int32_t)> rh = [&](int32_t id) { return nh(L.nstates[id]); };
        return L.nid.findOrInsert(nh(s), [&](int32_t id) { return L.nstates[id] == s; },
                                  [&]() { L.nstates.push_back(s); return (int32_t)L.nstates.size() - 1; }, rh);
    };
    auto internD = [&](Layer& L, const std::vector<int32_t>& set) -> int32_t {
        uint64_t h = hashInts(set.data(), set.size());
        std::function<uint64_t(int32_t)> rh = [&](int32_t id) { return L.setHash[id]; };
        auto eq = [&](int32_t id) {
            uint64_t a = L.setStart[id], b = L.setStart[id + 1];
            return b - a == set.size() && std::equal(set.begin(), set.end(), L.setPool.begin() + a);
        };
        return L.did.findOrInsert(h, eq, [&]() {
            L.setPool.insert(L.setPool.end(), set.begin(), set.end());
            L.setStart.push_back(L.setPool.size());
            L.setHash.push_back(h);
            return (int32_t)L.setHash.size() - 1;
        }, rh);
    };

    // the first LEFT_REACH columns produce no output
    std::vector<NState> cur{NState{in.start, 0, 0, 0}};
    for (int k = 0; k < LEFT_REACH; k++) {
        std::vector<NState> nxt;
        for (auto& s : cur) { expand(s); for (auto& [sy, t] : buf) if (sy == -1) nxt.push_back(t); }
        std::sort(nxt.begin(), nxt.end(), [](const NState& a, const NState& b) {
            return std::tie(a.q, a.cap, a.win, a.pend) < std::tie(b.q, b.cap, b.win, b.pend);
        });
        nxt.erase(std::unique(nxt.begin(), nxt.end()), nxt.end());
        cur.swap(nxt);
    }
    if (cur.empty()) { sink.empty(); return; }
    auto layer = std::make_unique<Layer>();
    {
        std::vector<int32_t> ids;
        for (auto& s : cur) ids.push_back(internN(*layer, s));
        std::sort(ids.begin(), ids.end());
        internD(*layer, ids);
    }
    std::array<std::vector<int32_t>, SYMBOLS> succ;
    std::vector<int32_t> set;
    uint8_t outSym[SYMBOLS];
    int32_t outTgt[SYMBOLS];
    while (true) {
        auto next = std::make_unique<Layer>();
        size_t count = layer->setHash.size();
        sink.beginLayer();
        // transitions of this layer's NFA states, computed once (targets are ids in the next layer)
        std::vector<int64_t> memoOff(layer->nstates.size(), -1);
        std::vector<uint16_t> memoLen(layer->nstates.size(), 0);
        std::vector<int8_t> memoSym;
        std::vector<int32_t> memoTgt;
        std::vector<char> acceptN(layer->nstates.size(), 0);
        for (size_t i = 0; i < layer->nstates.size(); i++) acceptN[i] = isAccept(layer->nstates[i]);
        for (size_t d = 0; d < count; d++) {
            set.assign(layer->setPool.begin() + layer->setStart[d], layer->setPool.begin() + layer->setStart[d + 1]);
            bool acc = false;
            for (auto& v : succ) v.clear();
            for (int32_t id : set) {
                if (acceptN[id]) acc = true;
                if (memoOff[id] < 0) {
                    expand(layer->nstates[id]);
                    auto local = buf;
                    memoOff[id] = (int64_t)memoSym.size();
                    memoLen[id] = (uint16_t)local.size();
                    for (auto& [sy, t] : local) { memoSym.push_back((int8_t)sy); memoTgt.push_back(internN(*next, t)); }
                }
                for (int64_t e = memoOff[id]; e < memoOff[id] + memoLen[id]; e++) if (memoSym[e] >= 0) succ[memoSym[e]].push_back(memoTgt[e]);
            }
            int k = 0;
            for (int a = 0; a < SYMBOLS; a++) {
                if (succ[a].empty()) continue;
                std::sort(succ[a].begin(), succ[a].end());
                succ[a].erase(std::unique(succ[a].begin(), succ[a].end()), succ[a].end());
                outSym[k] = (uint8_t)a;
                outTgt[k++] = internD(*next, succ[a]);
            }
            sink.state(acc, outSym, outTgt, k);
        }
        sink.endLayer();
        if (next->setHash.empty()) break;
        layer = std::move(next);
    }
}

// The next row's minimal DFA without holding the raw one in memory. Every accepted word has length W, so
// states of different layers are never equivalent: minimizing each layer against the already minimized
// next layer, last layer first, gives exactly the minimal DFA.
SDfa advanceRow(const std::vector<Orientation>& oris, SDfa& in, const std::string& scratch) {
    DiskSink sink;
    sink.f = fopen(scratch.c_str(), "w+b");
    if (!sink.f) fail("cannot open scratch file " + scratch);
    transduceLayered(oris, in, sink);
    in.release();
    if (sink.isEmpty) { fclose(sink.f); std::remove(scratch.c_str()); return SDfa::emptyLanguage(); }
    size_t L = sink.layerPos.size();
    struct MinLayer { std::vector<uint8_t> acc, sym; std::vector<uint32_t> off{0}; std::vector<int32_t> tgt; };
    std::vector<MinLayer> mins(L);
    std::vector<int32_t> clsNext;
    std::vector<uint8_t> acc, sym;
    std::vector<uint32_t> off;
    std::vector<int32_t> tgt;
    for (size_t d = L; d-- > 0;) {
        if (FSEEK(sink.f, sink.layerPos[d], SEEK_SET) != 0) fail("scratch file seek failed");
        uint64_t n = readValue<uint64_t>(sink.f, "scratch"), e = readValue<uint64_t>(sink.f, "scratch");
        acc.resize(n); off.resize(n + 1); sym.resize(e); tgt.resize(e);
        readAll(sink.f, acc.data(), n, "scratch");
        readAll(sink.f, off.data(), 4 * (n + 1), "scratch");
        readAll(sink.f, sym.data(), e, "scratch");
        readAll(sink.f, tgt.data(), 4 * e, "scratch");
        auto cn = [&](int32_t t) -> int32_t { return t < (int32_t)clsNext.size() ? clsNext[t] : -1; };
        std::vector<int32_t> cls(n, -1), rep;
        std::vector<uint64_t> repHash;
        IdTable table;
        std::function<uint64_t(int32_t)> rh = [&](int32_t c) { return repHash[c]; };
        for (uint64_t v = 0; v < n; v++) {
            bool useful = acc[v];
            uint64_t h = acc[v] ? HASH_ACCEPT : HASH_REJECT;
            for (uint32_t x = off[v]; x < off[v + 1]; x++) {
                int32_t c = cn(tgt[x]);
                if (c < 0) continue;
                useful = true;
                h = mixEdge(h, sym[x], c);
            }
            if (!useful) continue;
            auto sameAs = [&](int32_t c) {
                int32_t r = rep[c];
                if (acc[r] != acc[v]) return false;
                uint32_t a = off[r], ae = off[r + 1], b = off[v], be = off[v + 1];
                while (true) {
                    while (a < ae && cn(tgt[a]) < 0) a++;
                    while (b < be && cn(tgt[b]) < 0) b++;
                    if (a == ae || b == be) return a == ae && b == be;
                    if (sym[a] != sym[b] || cn(tgt[a]) != cn(tgt[b])) return false;
                    a++;
                    b++;
                }
            };
            cls[v] = table.findOrInsert(h, sameAs, [&]() { rep.push_back((int32_t)v); repHash.push_back(h); return (int32_t)rep.size() - 1; }, rh);
        }
        MinLayer& m = mins[d];
        m.acc.resize(rep.size());
        for (size_t c = 0; c < rep.size(); c++) {
            int32_t r = rep[c];
            m.acc[c] = acc[r];
            for (uint32_t x = off[r]; x < off[r + 1]; x++) {
                int32_t t = cn(tgt[x]);
                if (t < 0) continue;
                m.sym.push_back(sym[x]);
                m.tgt.push_back(t);
            }
            m.off.push_back((uint32_t)m.sym.size());
        }
        clsNext.swap(cls);
    }
    fclose(sink.f);
    std::remove(scratch.c_str());
    if (clsNext.empty() || clsNext[0] < 0) return SDfa::emptyLanguage();
    std::vector<int64_t> base(L + 1, 0);
    for (size_t d = 0; d < L; d++) base[d + 1] = base[d] + (int64_t)mins[d].acc.size();
    if (base[L] >= INT32_MAX) fail("the minimal DFA is too large for 32-bit ids");
    SDfa out;
    out.acc.reserve(base[L]);
    for (size_t d = 0; d < L; d++) {
        MinLayer& m = mins[d];
        for (size_t c = 0; c < m.acc.size(); c++) {
            out.acc.push_back(m.acc[c]);
            for (uint32_t x = m.off[c]; x < m.off[c + 1]; x++) { out.sym.push_back(m.sym[x]); out.tgt.push_back((int32_t)(base[d + 1] + m.tgt[x])); }
            out.off.push_back(out.sym.size());
        }
        m = MinLayer();
    }
    out.start = (int32_t)(base[0] + clsNext[0]);
    return out;
}

double countWords(const SDfa& d, int W) {
    std::vector<double> cur(d.size(), 0), nxt(d.size(), 0);
    cur[d.start] = 1;
    for (int len = 0; len < W; len++) {
        std::fill(nxt.begin(), nxt.end(), 0.0);
        for (size_t v = 0; v < d.size(); v++) if (cur[v] != 0) for (uint64_t e = d.off[v]; e < d.off[v + 1]; e++) nxt[d.tgt[e]] += cur[v];
        cur.swap(nxt);
    }
    double total = 0;
    for (size_t v = 0; v < d.size(); v++) if (d.acc[v]) total += cur[v];
    return total;
}

bool acceptsFlat(const SDfa& d, int W) {
    int32_t q = d.start;
    for (int i = 0; i < W && q >= 0; i++) q = d.next(q, 0);
    return q >= 0 && d.acc[q];
}

// Canonical encoding (BFS renumbering; edges are stored in increasing symbol order).
template <class Sink>
void canonicalWalk(const SDfa& d, Sink sink) {
    std::vector<int32_t> num(d.size(), -1);
    std::vector<int32_t> order{d.start};
    num[d.start] = 0;
    for (size_t i = 0; i < order.size(); i++) {
        int32_t v = order[i];
        sink(d.acc[v] ? 0xAULL : 0xBULL);
        for (uint64_t e = d.off[v]; e < d.off[v + 1]; e++) {
            int32_t t = d.tgt[e];
            if (num[t] < 0) { num[t] = (int32_t)order.size(); order.push_back(t); }
            sink((uint64_t)d.sym[e] << 40 | (uint64_t)num[t]);
        }
        sink(0xFFFFULL);
    }
}

std::pair<uint64_t, uint64_t> canonicalHash(const SDfa& d) {
    uint64_t h1 = 0x243F6A8885A308D3ULL, h2 = 0x13198A2E03707344ULL;
    canonicalWalk(d, [&](uint64_t v) {
        h1 ^= v + 0x9E3779B97F4A7C15ULL + (h1 << 6) + (h1 >> 2);
        h1 *= 0xBF58476D1CE4E5B9ULL;
        h2 += v * 0x94D049BB133111EBULL;
        h2 ^= h2 >> 31;
        h2 *= 0xD6E8FEB86659FD93ULL;
    });
    return {h1, h2};
}

std::vector<uint64_t> canonicalForm(const SDfa& d) {
    std::vector<uint64_t> enc;
    canonicalWalk(d, [&](uint64_t v) { enc.push_back(v); });
    return enc;
}

struct SeenRow { int32_t row; uint64_t h1, h2; };

// Everything needed to continue a run: row `row`'s language, the hashes and state counts of the rows before.
struct Checkpoint {
    int32_t width = 0, startRow = 0, row = 0;
    double seconds = 0;
    std::string pieceKey;
    std::vector<SeenRow> seen;
    std::vector<uint64_t> counts;
    SDfa lang;
};

void saveCheckpoint(const Checkpoint& c, const std::string& path) {
    std::string tmp = path + ".tmp";
    FILE* f = fopen(tmp.c_str(), "wb");
    if (!f) fail("cannot write checkpoint " + tmp);
    writeAll(f, CHECKPOINT_MAGIC, sizeof CHECKPOINT_MAGIC, "checkpoint");
    writeValue(f, c.width, "checkpoint");
    writeValue(f, c.startRow, "checkpoint");
    writeValue(f, c.row, "checkpoint");
    writeValue(f, c.seconds, "checkpoint");
    writeValue(f, (uint32_t)c.pieceKey.size(), "checkpoint");
    writeAll(f, c.pieceKey.data(), c.pieceKey.size(), "checkpoint");
    writeValue(f, (uint64_t)c.seen.size(), "checkpoint");
    for (auto& s : c.seen) { writeValue(f, s.row, "checkpoint"); writeValue(f, s.h1, "checkpoint"); writeValue(f, s.h2, "checkpoint"); }
    writeValue(f, (uint64_t)c.counts.size(), "checkpoint");
    writeAll(f, c.counts.data(), 8 * c.counts.size(), "checkpoint");
    const SDfa& d = c.lang;
    uint64_t n = d.size(), e = d.sym.size();
    writeValue(f, n, "checkpoint");
    writeValue(f, e, "checkpoint");
    writeValue(f, d.start, "checkpoint");
    writeAll(f, d.acc.data(), n, "checkpoint");
    std::vector<uint8_t> degree(n);
    for (uint64_t v = 0; v < n; v++) degree[v] = (uint8_t)(d.off[v + 1] - d.off[v]);  // at most SYMBOLS
    writeAll(f, degree.data(), n, "checkpoint");
    writeAll(f, d.sym.data(), e, "checkpoint");
    writeAll(f, d.tgt.data(), 4 * e, "checkpoint");
    writeAll(f, CHECKPOINT_END, sizeof CHECKPOINT_END, "checkpoint");
    if (fflush(f) != 0 || fclose(f) != 0) fail("cannot finish checkpoint " + tmp + "; is the disk full?");
    std::error_code error;
    std::filesystem::rename(tmp, path, error);
    if (error) fail("cannot replace checkpoint " + path + ": " + error.message());
}

Checkpoint loadCheckpoint(const std::string& path) {
    FILE* f = fopen(path.c_str(), "rb");
    if (!f) fail("cannot read checkpoint " + path);
    char magic[sizeof CHECKPOINT_MAGIC];
    readAll(f, magic, sizeof magic, "checkpoint");
    if (memcmp(magic, CHECKPOINT_MAGIC, sizeof magic) != 0) fail(path + " is not a checkpoint of this program");
    Checkpoint c;
    c.width = readValue<int32_t>(f, "checkpoint");
    c.startRow = readValue<int32_t>(f, "checkpoint");
    c.row = readValue<int32_t>(f, "checkpoint");
    c.seconds = readValue<double>(f, "checkpoint");
    c.pieceKey.resize(readValue<uint32_t>(f, "checkpoint"));
    readAll(f, c.pieceKey.data(), c.pieceKey.size(), "checkpoint");
    c.seen.resize(readValue<uint64_t>(f, "checkpoint"));
    for (auto& s : c.seen) { s.row = readValue<int32_t>(f, "checkpoint"); s.h1 = readValue<uint64_t>(f, "checkpoint"); s.h2 = readValue<uint64_t>(f, "checkpoint"); }
    c.counts.resize(readValue<uint64_t>(f, "checkpoint"));
    readAll(f, c.counts.data(), 8 * c.counts.size(), "checkpoint");
    SDfa& d = c.lang;
    uint64_t n = readValue<uint64_t>(f, "checkpoint"), e = readValue<uint64_t>(f, "checkpoint");
    d.start = readValue<int32_t>(f, "checkpoint");
    d.acc.resize(n);
    readAll(f, d.acc.data(), n, "checkpoint");
    std::vector<uint8_t> degree(n);
    readAll(f, degree.data(), n, "checkpoint");
    d.off.assign(n + 1, 0);
    for (uint64_t v = 0; v < n; v++) d.off[v + 1] = d.off[v] + degree[v];
    if (d.off[n] != e) fail(path + " is damaged (edge count)");
    d.sym.resize(e);
    d.tgt.resize(e);
    readAll(f, d.sym.data(), e, "checkpoint");
    readAll(f, d.tgt.data(), 4 * e, "checkpoint");
    char end[sizeof CHECKPOINT_END];
    readAll(f, end, sizeof end, "checkpoint");
    if (memcmp(end, CHECKPOINT_END, sizeof end) != 0) fail(path + " is damaged (no end mark)");
    fclose(f);
    if (n == 0 || d.start < 0 || (uint64_t)d.start >= n) fail(path + " is damaged (start state)");
    for (int32_t t : d.tgt) if (t < 0 || (uint64_t)t >= n) fail(path + " is damaged (edge target)");
    return c;
}

struct Options {
    int width = 0;
    int startRow = DEFAULT_START_ROW;
    std::vector<Cell> cells;
    std::string cellsText, checkpoint, scratch;
};

const char* USAGE =
    "usage: exact_width --width W --cells \"x,y x,y ...\" --scratch FILE [--checkpoint FILE] [--start-row R]\n"
    "Decides exactly whether the piece's width-W half-strip dies, tiles a rectangle or fills forever.\n"
    "With --checkpoint, resumes from FILE if it exists and rewrites it after every row. The scratch file\n"
    "holds one raw row.\n";

int parseInt(const std::string& text, const char* what) {
    try {
        size_t used = 0;
        int v = std::stoi(text, &used);
        if (used != text.size()) throw std::invalid_argument(text);
        return v;
    } catch (const std::exception&) {
        fail(std::string(what) + " must be a whole number, got '" + text + "'");
    }
}

std::vector<Cell> parseCells(const std::string& text) {
    std::vector<Cell> cells;
    std::istringstream in(text);
    std::string item;
    while (in >> item) {
        size_t comma = item.find(',');
        if (comma == std::string::npos) fail("cells look like \"0,0 1,0 2,0\", got '" + item + "'");
        int x = parseInt(item.substr(0, comma), "a cell x"), y = parseInt(item.substr(comma + 1), "a cell y");
        cells.push_back({y, x});
    }
    if (cells.empty()) fail("--cells is empty");
    if (std::set<Cell>(cells.begin(), cells.end()).size() != cells.size()) fail("--cells repeats a cell");
    return cells;
}

Options parseOptions(int argc, char** argv) {
    Options o;
    for (int i = 1; i < argc; i++) {
        std::string a = argv[i];
        if (a == "--help" || a == "-h") { std::cout << USAGE; std::exit(0); }
        if (i + 1 >= argc) fail("missing value after " + a + "\n" + USAGE);
        std::string v = argv[++i];
        if (a == "--width") o.width = parseInt(v, "--width");
        else if (a == "--start-row") o.startRow = parseInt(v, "--start-row");
        else if (a == "--cells") { o.cellsText = v; o.cells = parseCells(v); }
        else if (a == "--checkpoint") o.checkpoint = v;
        else if (a == "--scratch") o.scratch = v;
        else fail("unknown option " + a + "\n" + USAGE);
    }
    if (o.width < 1 || o.width > 2000) fail("--width must be between 1 and 2000");
    if (o.startRow < 0 || o.startRow > 12) fail("--start-row must be between 0 and 12");
    if (o.cells.empty() || o.scratch.empty()) fail(std::string("--width, --cells and --scratch are required\n") + USAGE);
    return o;
}

std::string pieceKeyOf(const std::vector<Orientation>& oris) {
    std::ostringstream key;
    for (auto& o : oris) {
        for (auto& c : o.cells) key << c.first << "," << c.second << " ";
        key << "|";
    }
    return key.str();
}

// The width-W words of the first rows, built width-independently up to startRow; stops early at a row
// whose width-W language is already empty or accepts the flat frontier. Returns that row and its language.
std::pair<int, SDfa> firstRow(const std::vector<Orientation>& oris, int startRow, int W) {
    Dfa lang;
    std::array<int32_t, SYMBOLS> row;
    row.fill(-1);
    lang.next = {row, row};
    lang.next[0][0] = 1;
    lang.next[1][0] = 1;
    lang.accept = {0, 1};
    for (int r = 0;; r++) {
        SDfa d = minimizeAcyclic(restrictLength(lang, W));
        if (r == startRow || countWords(d, W) == 0 || (r > 0 && acceptsFlat(d, W))) return {r, std::move(d)};
        lang = minimizeDense(nextLanguageDense(oris, lang));
        std::cerr << "universal row " << r + 1 << ": " << lang.size() << " states\n" << std::flush;
    }
}

void printCounts(const std::vector<uint64_t>& counts) {
    std::cout << "counts";
    for (auto c : counts) std::cout << " " << c;
    std::cout << "\n";
}

}  // namespace

int main(int argc, char** argv) {
    Options opt = parseOptions(argc, argv);
    auto oris = orientationsOf(opt.cells);
    const int W = opt.width;
    auto t0 = std::chrono::steady_clock::now();
    auto elapsed = [&]() { return std::chrono::duration<double>(std::chrono::steady_clock::now() - t0).count(); };

    Checkpoint c;
    const bool keepCheckpoints = !opt.checkpoint.empty();
    if (keepCheckpoints && std::filesystem::exists(opt.checkpoint)) {
        c = loadCheckpoint(opt.checkpoint);
        if (c.width != W || c.startRow != opt.startRow || c.pieceKey != pieceKeyOf(oris))
            fail(opt.checkpoint + " belongs to another width or piece");
    } else {
        c.width = W;
        c.startRow = opt.startRow;
        c.pieceKey = pieceKeyOf(oris);
        std::tie(c.row, c.lang) = firstRow(oris, opt.startRow, W);
    }
    double before = c.seconds;
    std::map<std::pair<uint64_t, uint64_t>, int32_t> seen;
    for (auto& s : c.seen) seen.emplace(std::make_pair(s.h1, s.h2), s.row);
    std::cout << "start " << c.row << "\n" << std::flush;

    while (true) {
        SDfa& d = c.lang;
        double words = countWords(d, W);
        c.counts.push_back(d.size());
        std::cout << "row " << c.row << " states " << d.size() << " words " << words << " seconds "
                  << (int64_t)(before + elapsed()) << "\n" << std::flush;
        if (words == 0) {
            printCounts(c.counts);
            std::cout << "dead " << c.row << "\n" << std::flush;
            return 0;
        }
        if (c.row > 0 && acceptsFlat(d, W)) {
            printCounts(c.counts);
            std::cout << "rectangle " << c.row << "\n" << std::flush;
            return 0;
        }
        auto h = canonicalHash(d);
        auto it = seen.find(h);
        if (it != seen.end()) {
            // a hash match is confirmed by rebuilding the earlier row and comparing exactly
            int32_t earlier = it->second;
            auto now = canonicalForm(d);
            auto [r0, e] = firstRow(oris, opt.startRow, W);
            for (int r = r0; r < earlier; r++) e = advanceRow(oris, e, opt.scratch);
            if (canonicalForm(e) == now) {
                printCounts(c.counts);
                std::cout << "forever " << earlier << " " << c.row << "\n" << std::flush;
                return 0;
            }
            std::cerr << "hash collision between rows " << earlier << " and " << c.row << "\n" << std::flush;
        }
        seen.emplace(h, c.row);
        c.seen.push_back({c.row, h.first, h.second});
        c.lang = advanceRow(oris, d, opt.scratch);
        c.row++;
        c.seconds = before + elapsed();
        if (keepCheckpoints) saveCheckpoint(c, opt.checkpoint);
    }
}
