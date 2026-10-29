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
//   lookahead <k>                                   rows ahead a kept frontier must be fillable (0: all kept)
//   start <row>                                     first row of this run (resumed or fresh)
//   row <r> states <n> words <x> seconds <s>        row r's language; seconds count every run so far
//   counts <n_start> <n_start+1> ...                state counts of every row, just before the verdict
//   dead <r> | rectangle <r> | forever <earlier> <r>
// Errors go to stderr with exit code 2.

#include <algorithm>
#include <array>
#include <atomic>
#include <chrono>
#include <condition_variable>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <filesystem>
#include <functional>
#include <iostream>
#include <map>
#include <memory>
#include <mutex>
#include <set>
#include <span>
#include <sstream>
#include <string>
#include <thread>
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
constexpr int MAX_THREADS = 256;
// rows this small keep their canonical form, so a repeat of one is confirmed without recomputing it
constexpr size_t SMALL_ROW_STATES = 1 << 15;
// kept rows this small stay in memory for the last `lookahead` rows, to find the exact death row
constexpr size_t RECENT_ROW_STATES = 1 << 22;

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

// Edge offsets in 5 bytes each instead of 8 (a 32-bit low part and an 8-bit high part, so up to 2^40
// edges): with about 2 edges per state this is a fifth of a row automaton's memory.
class Offsets {
public:
    Offsets() { push_back(0); }
    void push_back(uint64_t x) {
        if (x >> 40) fail("an automaton has too many edges for 40-bit offsets");
        lo.push_back((uint32_t)x);
        hi.push_back((uint8_t)(x >> 32));
    }
    uint64_t operator[](size_t i) const { return (uint64_t)hi[i] << 32 | lo[i]; }
    void clear() {
        std::vector<uint32_t>().swap(lo);
        std::vector<uint8_t>().swap(hi);
    }
    void reserve(size_t n) {
        lo.reserve(n);
        hi.reserve(n);
    }

private:
    std::vector<uint32_t> lo;
    std::vector<uint8_t> hi;
};

// Sparse layered DFA: edges of state v are off[v]..off[v+1], in increasing symbol order.
struct SDfa {
    Offsets off;
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
    explicit IdTable(size_t initialSlots = 1 << 16) { slots.assign(initialSlots, -1); }
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

// A fixed set of worker threads that run one parallel loop at a time; the calling thread works too.
class Workers {
public:
    explicit Workers(int threads) : count(std::max(1, threads)) {
        for (int i = 1; i < count; i++) pool.emplace_back([this, i] { loop(i); });
    }
    ~Workers() {
        {
            std::lock_guard<std::mutex> lock(m);
            stop = true;
        }
        wake.notify_all();
        for (auto& t : pool) t.join();
    }
    Workers(const Workers&) = delete;
    Workers& operator=(const Workers&) = delete;

    int size() const { return count; }

    // body(begin, end) on chunks of [0, n), `grain` items each, spread over the threads
    void forRange(size_t n, size_t grain, const std::function<void(size_t, size_t)>& body) {
        forRange(n, grain, [&](size_t b, size_t e, int) { body(b, e); });
    }

    // the same, with the index (0..size()-1) of the thread running each chunk
    void forRange(size_t n, size_t grain, const std::function<void(size_t, size_t, int)>& body) {
        if (n == 0) return;
        grain = std::max<size_t>(1, grain);
        if (count == 1 || n <= grain) { body(0, n, 0); return; }
        {
            std::lock_guard<std::mutex> lock(m);
            job = &body;
            total = n;
            chunk = grain;
            nextItem = 0;
            busy = count - 1;
            generation++;
        }
        wake.notify_all();
        work(0);
        std::unique_lock<std::mutex> lock(m);
        finished.wait(lock, [&] { return busy == 0; });
        job = nullptr;
    }

private:
    int count;
    std::vector<std::thread> pool;
    std::mutex m;
    std::condition_variable wake, finished;
    const std::function<void(size_t, size_t, int)>* job = nullptr;
    size_t total = 0, chunk = 1;
    std::atomic<size_t> nextItem{0};
    int busy = 0;
    uint64_t generation = 0;
    bool stop = false;

    void work(int worker) {
        while (true) {
            size_t begin = nextItem.fetch_add(chunk);
            if (begin >= total) return;
            (*job)(begin, std::min(total, begin + chunk), worker);
        }
    }

    void loop(int worker) {
        uint64_t seen = 0;
        while (true) {
            std::unique_lock<std::mutex> lock(m);
            wake.wait(lock, [&] { return stop || generation != seen; });
            if (stop) return;
            seen = generation;
            lock.unlock();
            work(worker);
            lock.lock();
            if (--busy == 0) finished.notify_one();
        }
    }
};

// Distinct items interned from many threads at once, in SHARDS separately locked tables. An id is
// (index in its shard << SHARD_BITS | shard) until finish() numbers all items densely, shard by shard.
constexpr int SHARD_BITS = 8;
constexpr uint32_t SHARDS = 1u << SHARD_BITS;
constexpr uint64_t SHARD_LIMIT = 1ULL << (32 - SHARD_BITS);  // items per shard
constexpr size_t SHARD_SLOTS = 1 << 8;

inline uint32_t shardOf(uint64_t h) { return (uint32_t)(h >> (64 - SHARD_BITS)); }

class ShardOffsets {
public:
    std::array<uint64_t, SHARDS + 1> start{};
    int32_t dense(uint32_t sparse) const { return (int32_t)(start[sparse & (SHARDS - 1)] + (sparse >> SHARD_BITS)); }
    uint64_t total() const { return start[SHARDS]; }
    // the shard holding dense index i (the last k with start[k] <= i, so never an empty shard)
    uint32_t shardOf(uint64_t i) const {
        return (uint32_t)(std::upper_bound(start.begin(), start.end(), i) - start.begin() - 1);
    }
};

// A layer's items as the shards built them, moved in rather than concatenated: dense index i is item
// i - offsets.start[k] of segment k.
template <class T>
class Segmented {
public:
    std::array<std::vector<T>, SHARDS> segment;
    ShardOffsets offsets;
    Segmented() = default;
    explicit Segmented(std::vector<T> items) {
        for (uint32_t k = 1; k <= SHARDS; k++) offsets.start[k] = items.size();
        segment[0] = std::move(items);
    }
    size_t size() const { return offsets.total(); }
    bool empty() const { return size() == 0; }
    const T& operator[](uint64_t i) const {
        uint32_t k = offsets.shardOf(i);
        return segment[k][i - offsets.start[k]];
    }
};

// Sets of int32 the same way: each shard's pool and set offsets moved in; a set lies in one shard.
class SegmentedSets {
public:
    std::array<std::vector<int32_t>, SHARDS> pool;
    std::array<std::vector<uint64_t>, SHARDS> start;  // start[k][j]..start[k][j + 1] in pool[k]
    ShardOffsets offsets;
    explicit SegmentedSets(std::vector<int32_t> set) {
        for (uint32_t k = 1; k <= SHARDS; k++) offsets.start[k] = 1;
        start[0] = {0, set.size()};
        pool[0] = std::move(set);
    }
    SegmentedSets() = default;
    size_t size() const { return offsets.total(); }
    std::span<const int32_t> operator[](uint64_t d) const {
        uint32_t k = offsets.shardOf(d);
        uint64_t j = d - offsets.start[k];
        return {pool[k].data() + start[k][j], start[k][j + 1] - start[k][j]};
    }
};

template <class T, class Hash>
class ShardedIds {
public:
    ShardedIds() {
        for (auto& s : shards) s.rehash = [&s](int32_t id) { return Hash{}(s.items[id]); };
    }
    uint32_t intern(const T& item) {
        uint64_t h = Hash{}(item);
        uint32_t k = shardOf(h);
        Shard& s = shards[k];
        std::lock_guard<std::mutex> lock(s.m);
        int32_t local = s.table.findOrInsert(
            h, [&](int32_t id) { return s.items[id] == item; },
            [&]() {
                if (s.items.size() >= SHARD_LIMIT) fail("too many states in one layer for 32-bit ids");
                s.items.push_back(item);
                return (int32_t)s.items.size() - 1;
            },
            s.rehash);
        return (uint32_t)local << SHARD_BITS | k;
    }
    // every item in dense order (its offsets also map interned ids to dense ones); empties the shards
    Segmented<T> finish() {
        Segmented<T> out;
        for (uint32_t k = 0; k < SHARDS; k++) out.offsets.start[k + 1] = out.offsets.start[k] + shards[k].items.size();
        if (out.offsets.total() >= INT32_MAX) fail("too many states in one layer for 32-bit ids");
        for (uint32_t k = 0; k < SHARDS; k++) {
            out.segment[k] = std::move(shards[k].items);
            shards[k].items.clear();  // moved-from: make it certainly empty
            shards[k].table = IdTable(SHARD_SLOTS);
        }
        return out;
    }

private:
    struct Shard {
        std::mutex m;
        IdTable table{SHARD_SLOTS};
        std::vector<T> items;
        std::function<uint64_t(int32_t)> rehash;
    };
    std::array<Shard, SHARDS> shards;
};

uint64_t hashInts(const int32_t* p, size_t n);

// Sets of int32 (sorted, as the caller hands them in) interned like ShardedIds.
class ShardedSets {
public:
    ShardedSets() {
        for (auto& s : shards) s.rehash = [&s](int32_t id) { return s.hash[id]; };
    }
    uint32_t intern(const std::vector<int32_t>& set) {
        uint64_t h = hashInts(set.data(), set.size());
        uint32_t k = shardOf(h);
        Shard& s = shards[k];
        std::lock_guard<std::mutex> lock(s.m);
        auto same = [&](int32_t id) {
            uint64_t a = s.start[id], b = s.start[id + 1];
            return b - a == set.size() && std::equal(set.begin(), set.end(), s.pool.begin() + a);
        };
        int32_t local = s.table.findOrInsert(h, same, [&]() {
            if (s.hash.size() >= SHARD_LIMIT) fail("too many states in one layer for 32-bit ids");
            s.pool.insert(s.pool.end(), set.begin(), set.end());
            s.start.push_back(s.pool.size());
            s.hash.push_back(h);
            return (int32_t)s.hash.size() - 1;
        }, s.rehash);
        return (uint32_t)local << SHARD_BITS | k;
    }
    bool empty() const {
        for (auto& s : shards) if (!s.hash.empty()) return false;
        return true;
    }
    // every set in dense order (its offsets also map interned ids to dense ones); empties the shards
    SegmentedSets finish() {
        SegmentedSets out;
        for (uint32_t k = 0; k < SHARDS; k++) out.offsets.start[k + 1] = out.offsets.start[k] + shards[k].hash.size();
        if (out.offsets.total() >= INT32_MAX) fail("too many states in one layer for 32-bit ids");
        for (uint32_t k = 0; k < SHARDS; k++) {
            Shard& s = shards[k];
            out.pool[k] = std::move(s.pool);
            s.pool.clear();
            out.start[k] = std::move(s.start);
            s.start.assign(1, 0);
            std::vector<uint64_t>().swap(s.hash);
            s.table = IdTable(SHARD_SLOTS);
        }
        return out;
    }

private:
    struct Shard {
        std::mutex m;
        IdTable table{SHARD_SLOTS};
        std::vector<int32_t> pool;
        std::vector<uint64_t> start{0};
        std::vector<uint64_t> hash;
        std::function<uint64_t(int32_t)> rehash;
    };
    std::array<Shard, SHARDS> shards;
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

// The transducer's output layers in a scratch file, each as n, e, acc[n], off[n+1] (uint32), sym[e],
// tgt[e] (local ids in the next layer); writeLayer() streams them there straight from the tasks' buffers.
struct DiskSink {
    FILE* f = nullptr;
    bool isEmpty = false;
    std::vector<int64_t> layerPos;
    void empty() { isEmpty = true; }
};

// One layer of a layered DFA: the states after d symbols, with edges into the next layer (local ids).
struct Layer {
    std::vector<uint8_t> acc, sym;
    std::vector<uint32_t> off{0};
    std::vector<int32_t> tgt;
    size_t size() const { return acc.size(); }
};

// Files of one run: <scratch>.<n> for each row automaton and raw transducer output. Files a killed run
// left behind are removed when the next run with the same --scratch starts.
class ScratchFiles {
public:
    explicit ScratchFiles(std::string base) : base(std::move(base)) {}
    std::string fresh() { return base + "." + std::to_string(next++); }
    void removeLeftovers() const {
        namespace fs = std::filesystem;
        const fs::path p(base);
        const fs::path dir = p.has_parent_path() ? p.parent_path() : fs::path(".");
        const std::string prefix = p.filename().string() + ".";
        std::error_code error;
        for (const auto& entry : fs::directory_iterator(dir, error)) {
            const std::string name = entry.path().filename().string();
            if (name.size() <= prefix.size() || name.compare(0, prefix.size(), prefix) != 0) continue;
            if (!std::all_of(name.begin() + prefix.size(), name.end(), [](char ch) { return ch >= '0' && ch <= '9'; }))
                continue;
            fs::remove(entry.path(), error);
        }
    }

private:
    std::string base;
    uint64_t next = 0;
};

// A layered DFA of width-W words kept in a file and read one layer at a time, so a row never has to fit in
// memory: every edge goes from layer d to layer d + 1, and states and targets are numbered within their layer.
// Layers may be written in any order. The file is deleted with the object.
class LayeredDfa {
public:
    int32_t start = 0;  // a state of layer 0

    LayeredDfa() = default;
    explicit LayeredDfa(std::string file) : path(std::move(file)) {
        f = fopen(path.c_str(), "w+b");
        if (!f) fail("cannot open scratch file " + path);
    }
    LayeredDfa(const LayeredDfa&) = delete;
    LayeredDfa& operator=(const LayeredDfa&) = delete;
    LayeredDfa(LayeredDfa&& o) noexcept { take(o); }
    LayeredDfa& operator=(LayeredDfa&& o) noexcept {
        if (this != &o) {
            reset();
            take(o);
        }
        return *this;
    }
    ~LayeredDfa() { reset(); }

    void reset() {  // deletes the file
        if (f) {
            fclose(f);
            std::remove(path.c_str());
            f = nullptr;
        }
        layers.clear();
        start = 0;
        symbols = 0;
    }
    size_t depth() const { return layers.size(); }
    uint64_t statesAt(size_t d) const { return d < layers.size() ? layers[d].states : 0; }
    uint64_t size() const {
        uint64_t n = 0;
        for (const Info& l : layers) n += l.states;
        return n;
    }
    uint64_t edges() const {
        uint64_t e = 0;
        for (const Info& l : layers) e += l.edges;
        return e;
    }
    uint64_t symbolsUsed() const { return symbols; }  // bit s: some edge reads symbol s

    void put(size_t d, const uint8_t* acc, const uint32_t* off, const uint8_t* sym, const int32_t* tgt, uint64_t n) {
        const uint64_t e = off[n];
        if (FSEEK(f, 0, SEEK_END) != 0) fail("scratch file seek failed");
        if (d >= layers.size()) layers.resize(d + 1);
        layers[d] = {FTELL(f), n, e};
        writeAll(f, acc, n, "row automaton");
        writeAll(f, off, 4 * (n + 1), "row automaton");
        writeAll(f, sym, e, "row automaton");
        writeAll(f, tgt, 4 * e, "row automaton");
        for (uint64_t x = 0; x < e; x++) symbols |= 1ULL << sym[x];
    }
    void put(size_t d, const Layer& l) { put(d, l.acc.data(), l.off.data(), l.sym.data(), l.tgt.data(), l.size()); }

    // layer d (empty past the last layer)
    void get(size_t d, Layer& l) const {
        const uint64_t n = statesAt(d), e = d < layers.size() ? layers[d].edges : 0;
        l.acc.resize(n);
        l.off.assign(n + 1, 0);
        l.sym.resize(e);
        l.tgt.resize(e);
        if (n == 0) return;
        if (FSEEK(f, layers[d].pos, SEEK_SET) != 0) fail("scratch file seek failed");
        readAll(f, l.acc.data(), n, "row automaton");
        readAll(f, l.off.data(), 4 * (n + 1), "row automaton");
        readAll(f, l.sym.data(), e, "row automaton");
        readAll(f, l.tgt.data(), 4 * e, "row automaton");
    }

    LayeredDfa copy(std::string file) const {
        LayeredDfa out(std::move(file));
        Layer l;
        for (size_t d = 0; d < depth(); d++) {
            get(d, l);
            out.put(d, l);
        }
        out.start = start;
        return out;
    }

    static LayeredDfa emptyLanguage(std::string file) {
        LayeredDfa out(std::move(file));
        const uint8_t acc = 0;
        const uint32_t off[2] = {0, 0};
        out.put(0, &acc, off, nullptr, nullptr, 1);
        return out;
    }

private:
    struct Info { int64_t pos = 0; uint64_t states = 0, edges = 0; };
    FILE* f = nullptr;
    std::string path;
    std::vector<Info> layers;
    uint64_t symbols = 0;

    void take(LayeredDfa& o) {
        f = o.f;
        path = std::move(o.path);
        layers = std::move(o.layers);
        start = o.start;
        symbols = o.symbols;
        o.f = nullptr;
        o.layers.clear();
    }
};

// A small in-memory layered DFA (the first rows) as a LayeredDfa, its states renumbered layer by layer.
LayeredDfa toLayered(const SDfa& s, std::string file) {
    LayeredDfa out(std::move(file));
    std::vector<int32_t> local(s.size(), -1), layer{s.start}, next;
    local[s.start] = 0;
    Layer l;
    for (size_t d = 0; !layer.empty(); d++) {
        next.clear();
        l.acc.clear();
        l.sym.clear();
        l.tgt.clear();
        l.off.assign(1, 0);
        for (int32_t v : layer) {
            l.acc.push_back(s.acc[v]);
            for (uint64_t e = s.off[v]; e < s.off[v + 1]; e++) {
                int32_t t = s.tgt[e];
                if (local[t] < 0) {
                    local[t] = (int32_t)next.size();
                    next.push_back(t);
                }
                l.sym.push_back(s.sym[e]);
                l.tgt.push_back(local[t]);
            }
            l.off.push_back((uint32_t)l.sym.size());
        }
        out.put(d, l);
        layer.swap(next);
    }
    out.start = 0;
    return out;
}

// The words Lookahead::keep() keeps, as a minimal DFA over a small frontier alphabet (Lookahead::buildFilter).
struct WindowFilter {
    std::array<int8_t, 64> index;  // symbol -> letter, -1 outside the alphabet
    int m = 0;
    std::vector<int32_t> next;  // state * m + letter
    std::vector<uint8_t> acc;
    int32_t start = 0, dead = 0;
    size_t states = 0;
};

// One row of the transducer over a layered DFA, layer by layer into the sink. Each layer is a subset
// construction: every transducer state of the layer is expanded once (in parallel), then every subset state
// collects its successor sets per output symbol (in parallel); new states and sets of the next layer go to
// sharded tables and are numbered densely once the layer is done. With a window filter each subset state also
// carries the filter's state (as a last, negative element -(state + 1) of its set), and an edge that completes a
// bad window is never made: the row comes out already pruned. A symbol outside the filter's alphabet sets
// *uncovered (the caller then builds the row without the filter).
constexpr size_t EXPAND_CHUNK = 1 << 12;  // transducer states per task
constexpr size_t SUBSET_CHUNK = 1 << 10;  // subset states per task
constexpr size_t MINIMIZE_CHUNK = 1 << 14;  // raw states per task when minimizing

struct LayerOutput {  // one task's share of a layer, in subset-state order
    std::vector<uint8_t> acc, degree, sym;
    std::vector<uint32_t> tgt;  // interned ids until the next layer is numbered
};

void writeLayer(std::vector<LayerOutput>& parts, const ShardOffsets& next, DiskSink& sink, Workers& workers) {
    workers.forRange(parts.size(), 1, [&](size_t b, size_t e) {
        for (size_t p = b; p < e; p++) for (auto& t : parts[p].tgt) t = (uint32_t)next.dense(t);
    });
    uint64_t n = 0, e = 0;
    for (const auto& part : parts) {
        n += part.acc.size();
        e += part.sym.size();
    }
    if (e > UINT32_MAX) fail("a layer has too many edges for 32-bit offsets");
    sink.layerPos.push_back(FTELL(sink.f));
    writeValue(sink.f, n, "scratch");
    writeValue(sink.f, e, "scratch");
    for (const auto& part : parts) writeAll(sink.f, part.acc.data(), part.acc.size(), "scratch");
    uint32_t edges = 0;
    writeValue(sink.f, edges, "scratch");
    std::vector<uint32_t> off;
    for (auto& part : parts) {
        off.resize(part.degree.size());
        for (size_t i = 0; i < off.size(); i++) off[i] = edges += part.degree[i];
        writeAll(sink.f, off.data(), 4 * off.size(), "scratch");
        std::vector<uint8_t>().swap(part.degree);
        std::vector<uint8_t>().swap(part.acc);
    }
    for (auto& part : parts) {
        writeAll(sink.f, part.sym.data(), part.sym.size(), "scratch");
        std::vector<uint8_t>().swap(part.sym);
    }
    for (auto& part : parts) {
        writeAll(sink.f, part.tgt.data(), 4 * part.tgt.size(), "scratch");
        std::vector<uint32_t>().swap(part.tgt);
    }
}

void transduceLayered(const std::vector<Orientation>& oris, const LayeredDfa& in, DiskSink& sink, Workers& workers,
                      const WindowFilter* filter, std::atomic<bool>& uncovered) {
    // every transducer state of a layer has read the same number of input columns: its q is a state of the
    // input layer `inputDepth` (or a flush state after the last column)
    Layer input;
    size_t inputDepth = 0;
    in.get(0, input);
    auto expand = [&](const NState& s, std::vector<std::pair<int, NState>>& buf) {
        buf.clear();
        if (s.q >= 0) {
            if ((size_t)s.q >= input.size()) fail("a transducer state lies outside its input layer");
            for (uint32_t e = input.off[s.q]; e < input.off[s.q + 1]; e++)
                stepColumn(oris, s, input.sym[e], input.tgt[e], buf);
        }
        flushStep(s, s.q >= 0 && input.acc[s.q], buf);
    };
    auto isAccept = [&](const NState& s) { return s.q == FLUSH_BASE - 3; };
    auto order = [](const NState& a, const NState& b) {
        return std::tie(a.q, a.cap, a.win, a.pend) < std::tie(b.q, b.cap, b.win, b.pend);
    };

    // the first LEFT_REACH columns produce no output
    std::vector<NState> cur{NState{in.start, 0, 0, 0}};
    std::vector<std::pair<int, NState>> buf;
    for (int k = 0; k < LEFT_REACH; k++) {
        std::vector<NState> nxt;
        for (auto& s : cur) { expand(s, buf); for (auto& [sy, t] : buf) if (sy == -1) nxt.push_back(t); }
        std::sort(nxt.begin(), nxt.end(), order);
        nxt.erase(std::unique(nxt.begin(), nxt.end()), nxt.end());
        cur.swap(nxt);
        in.get(++inputDepth, input);
    }
    if (cur.empty()) { sink.empty(); return; }

    // the current layer: its transducer states, and its subset states as sets of their ids
    std::vector<int32_t> first(cur.size());
    for (size_t i = 0; i < cur.size(); i++) first[i] = (int32_t)i;
    if (filter) first.push_back(-(filter->start + 1));
    Segmented<NState> nstates(std::move(cur));
    SegmentedSets setsOf(std::move(first));

    while (true) {
        // every transducer state's moves, with targets as ids of the next layer
        size_t n = nstates.size(), chunks = (n + EXPAND_CHUNK - 1) / EXPAND_CHUNK;
        std::vector<std::vector<uint8_t>> moveSym(chunks);
        std::vector<std::vector<uint32_t>> moveTgt(chunks);
        std::vector<uint32_t> moveStart(n);
        std::vector<uint16_t> moveCount(n);
        std::vector<char> accepting(n);
        auto nextStates = std::make_unique<ShardedIds<NState, NStateHash>>();
        workers.forRange(chunks, 1, [&](size_t b, size_t e) {
            std::vector<std::pair<int, NState>> local;
            for (size_t c = b; c < e; c++) {
                const size_t lo = c * EXPAND_CHUNK, hi = std::min(n, (c + 1) * EXPAND_CHUNK);
                uint32_t k = nstates.offsets.shardOf(lo);
                for (size_t id = lo; id < hi; id++) {
                    while (id >= nstates.offsets.start[k + 1]) k++;
                    const NState& from = nstates.segment[k][id - nstates.offsets.start[k]];
                    accepting[id] = isAccept(from);
                    expand(from, local);
                    moveStart[id] = (uint32_t)moveSym[c].size();
                    for (auto& [sy, t] : local) {
                        if (sy < 0) continue;
                        moveSym[c].push_back((uint8_t)sy);
                        moveTgt[c].push_back(nextStates->intern(t));
                    }
                    moveCount[id] = (uint16_t)(moveSym[c].size() - moveStart[id]);
                }
            }
        });
        Segmented<NState> following = nextStates->finish();
        nextStates.reset();
        in.get(++inputDepth, input);
        workers.forRange(chunks, 1, [&](size_t b, size_t e) {
            for (size_t c = b; c < e; c++) for (auto& t : moveTgt[c]) t = (uint32_t)following.offsets.dense(t);
        });

        // every subset state's successor set per symbol
        size_t sets = setsOf.size(), parts = (sets + SUBSET_CHUNK - 1) / SUBSET_CHUNK;
        std::vector<LayerOutput> out(parts);
        auto nextSets = std::make_unique<ShardedSets>();
        workers.forRange(parts, 1, [&](size_t b, size_t e) {
            std::array<std::vector<int32_t>, SYMBOLS> succ;
            for (size_t p = b; p < e; p++) {
                LayerOutput& o = out[p];
                for (size_t d = p * SUBSET_CHUNK; d < std::min(sets, (p + 1) * SUBSET_CHUNK); d++) {
                    bool acc = false;
                    for (auto& v : succ) v.clear();
                    std::span<const int32_t> set = setsOf[d];
                    size_t end = set.size();
                    int32_t state = 0;  // the filter's
                    if (filter) state = -set[--end] - 1;
                    for (size_t i = 0; i < end; i++) {
                        int32_t id = set[i];
                        if (accepting[id]) acc = true;
                        const size_t c = id / EXPAND_CHUNK;
                        for (uint32_t x = moveStart[id]; x < moveStart[id] + moveCount[id]; x++)
                            succ[moveSym[c][x]].push_back((int32_t)moveTgt[c][x]);
                    }
                    uint8_t degree = 0;
                    for (int a = 0; a < SYMBOLS; a++) {
                        if (succ[a].empty()) continue;
                        std::sort(succ[a].begin(), succ[a].end());
                        succ[a].erase(std::unique(succ[a].begin(), succ[a].end()), succ[a].end());
                        if (filter) {
                            int letter = filter->index[a];
                            if (letter < 0) { uncovered = true; continue; }
                            int32_t next = filter->next[(size_t)state * filter->m + letter];
                            if (next == filter->dead) continue;
                            succ[a].push_back(-(next + 1));
                        }
                        o.sym.push_back((uint8_t)a);
                        o.tgt.push_back(nextSets->intern(succ[a]));
                        degree++;
                    }
                    o.acc.push_back(acc && (!filter || filter->acc[state]));
                    o.degree.push_back(degree);
                }
            }
        });
        std::vector<std::vector<uint8_t>>().swap(moveSym);
        std::vector<std::vector<uint32_t>>().swap(moveTgt);
        bool last = nextSets->empty();
        setsOf = nextSets->finish();
        nextSets.reset();
        writeLayer(out, setsOf.offsets, sink, workers);
        if (last) break;
        nstates = std::move(following);
    }
}

// The next row's minimal DFA without holding the raw one in memory. Every accepted word has length W, so
// states of different layers are never equivalent: minimizing each layer against the already minimized
// next layer, last layer first, gives exactly the minimal DFA.
LayeredDfa minimizeFromDisk(DiskSink& sink, const std::string& raw, std::string file, Workers& workers);

LayeredDfa advanceRow(const std::vector<Orientation>& oris, const LayeredDfa& in, ScratchFiles& files, Workers& workers,
                      const WindowFilter* filter = nullptr, bool* uncovered = nullptr) {
    DiskSink sink;
    const std::string raw = files.fresh();
    sink.f = fopen(raw.c_str(), "w+b");
    if (!sink.f) fail("cannot open scratch file " + raw);
    auto t0 = std::chrono::steady_clock::now();
    std::atomic<bool> missed{false};
    transduceLayered(oris, in, sink, workers, filter, missed);
    if (uncovered) *uncovered = missed;
    auto t1 = std::chrono::steady_clock::now();
    LayeredDfa out = minimizeFromDisk(sink, raw, files.fresh(), workers);
    auto t2 = std::chrono::steady_clock::now();
    std::cerr << "    seconds: transduce " << std::chrono::duration<double>(t1 - t0).count() << " minimize "
              << std::chrono::duration<double>(t2 - t1).count() << "\n" << std::flush;
    return out;
}

// The minimal DFA of the raw layers in a scratch file (each layer's targets are local ids in the next layer),
// minimized last layer first, each minimized layer written straight to `file`; deletes the raw file.
LayeredDfa minimizeFromDisk(DiskSink& sink, const std::string& raw, std::string file, Workers& workers) {
    if (sink.isEmpty) {
        fclose(sink.f);
        std::remove(raw.c_str());
        return LayeredDfa::emptyLanguage(std::move(file));
    }
    size_t L = sink.layerPos.size();
    const std::string name = file;
    LayeredDfa out(std::move(file));
    std::vector<int32_t> clsNext;
    std::vector<uint8_t> acc, sym, minAcc, minSym;
    std::vector<uint32_t> off, minOff;
    std::vector<int32_t> tgt, minTgt;
    for (size_t d = L; d-- > 0;) {
        if (FSEEK(sink.f, sink.layerPos[d], SEEK_SET) != 0) fail("scratch file seek failed");
        uint64_t n = readValue<uint64_t>(sink.f, "scratch"), e = readValue<uint64_t>(sink.f, "scratch");
        acc.resize(n); off.resize(n + 1); sym.resize(e); tgt.resize(e);
        readAll(sink.f, acc.data(), n, "scratch");
        readAll(sink.f, off.data(), 4 * (n + 1), "scratch");
        readAll(sink.f, sym.data(), e, "scratch");
        readAll(sink.f, tgt.data(), 4 * e, "scratch");
        // every target as a class of the next layer, or -1 where it leads nowhere: looked up once
        workers.forRange(e, MINIMIZE_CHUNK, [&](size_t b, size_t end) {
            const int32_t known = (int32_t)clsNext.size();
            for (size_t x = b; x < end; x++) tgt[x] = tgt[x] < known ? clsNext[tgt[x]] : -1;
        });
        // states with the same acceptance and the same edges into the next layer's classes are one class;
        // classes are found in sharded tables (in parallel), then numbered densely shard by shard
        struct ClassShard {
            std::mutex m;
            IdTable table{SHARD_SLOTS};
            std::vector<int32_t> rep;
            std::vector<uint64_t> hash;
            std::function<uint64_t(int32_t)> rehash;
        };
        auto shards = std::make_unique<std::array<ClassShard, SHARDS>>();
        for (auto& sh : *shards) sh.rehash = [&sh](int32_t c) { return sh.hash[c]; };
        std::vector<int32_t> cls(n, -1);
        std::vector<uint32_t> sparse(n);
        workers.forRange(n, MINIMIZE_CHUNK, [&](size_t b, size_t end) {
            for (uint64_t v = b; v < end; v++) {
                bool useful = acc[v];
                uint64_t h = acc[v] ? HASH_ACCEPT : HASH_REJECT;
                for (uint32_t x = off[v]; x < off[v + 1]; x++) {
                    if (tgt[x] < 0) continue;
                    useful = true;
                    h = mixEdge(h, sym[x], tgt[x]);
                }
                if (!useful) continue;
                uint32_t k = shardOf(h);
                ClassShard& sh = (*shards)[k];
                auto sameAs = [&](int32_t c) {
                    int32_t r = sh.rep[c];
                    if (acc[r] != acc[v]) return false;
                    uint32_t a = off[r], ae = off[r + 1], bb = off[v], be = off[v + 1];
                    while (true) {
                        while (a < ae && tgt[a] < 0) a++;
                        while (bb < be && tgt[bb] < 0) bb++;
                        if (a == ae || bb == be) return a == ae && bb == be;
                        if (sym[a] != sym[bb] || tgt[a] != tgt[bb]) return false;
                        a++;
                        bb++;
                    }
                };
                std::lock_guard<std::mutex> lock(sh.m);
                int32_t local = sh.table.findOrInsert(h, sameAs, [&]() {
                    if (sh.rep.size() >= SHARD_LIMIT) fail("too many states in one layer for 32-bit ids");
                    sh.rep.push_back((int32_t)v);
                    sh.hash.push_back(h);
                    return (int32_t)sh.rep.size() - 1;
                }, sh.rehash);
                sparse[v] = (uint32_t)local << SHARD_BITS | k;
                cls[v] = 0;  // useful; numbered below
            }
        });
        ShardOffsets ids;
        for (uint32_t k = 0; k < SHARDS; k++) ids.start[k + 1] = ids.start[k] + (*shards)[k].rep.size();
        Segmented<int32_t> rep;
        rep.offsets = ids;
        for (uint32_t k = 0; k < SHARDS; k++) rep.segment[k] = std::move((*shards)[k].rep);
        shards.reset();
        workers.forRange(n, MINIMIZE_CHUNK, [&](size_t b, size_t end) {
            for (size_t v = b; v < end; v++) if (cls[v] >= 0) cls[v] = ids.dense(sparse[v]);
        });
        std::vector<uint32_t>().swap(sparse);
        // exact sizes first (edge counts per class, then their prefix sums), so the layer is filled in place
        size_t classes = rep.size();
        minAcc.resize(classes);
        minOff.assign(classes + 1, 0);
        workers.forRange(classes, MINIMIZE_CHUNK, [&](size_t b, size_t end) {
            for (size_t c = b; c < end; c++) {
                int32_t r = rep[c];
                minAcc[c] = acc[r];
                uint32_t kept = 0;
                for (uint32_t x = off[r]; x < off[r + 1]; x++) kept += tgt[x] >= 0;
                minOff[c + 1] = kept;
            }
        });
        for (size_t c = 0; c < classes; c++) minOff[c + 1] += minOff[c];
        minSym.resize(minOff[classes]);
        minTgt.resize(minOff[classes]);
        workers.forRange(classes, MINIMIZE_CHUNK, [&](size_t b, size_t end) {
            for (size_t c = b; c < end; c++) {
                int32_t r = rep[c];
                uint32_t at = minOff[c];
                for (uint32_t x = off[r]; x < off[r + 1]; x++) {
                    if (tgt[x] < 0) continue;
                    minSym[at] = sym[x];
                    minTgt[at++] = tgt[x];
                }
            }
        });
        out.put(d, minAcc.data(), minOff.data(), minSym.data(), minTgt.data(), classes);
        clsNext.swap(cls);
    }
    fclose(sink.f);
    std::remove(raw.c_str());
    if (clsNext.empty() || clsNext[0] < 0) {
        out.reset();
        return LayeredDfa::emptyLanguage(name);
    }
    out.start = clsNext[0];
    return out;
}

// Words of length W, one pass over the layers.
double countWords(const LayeredDfa& d, int W) {
    Layer layer;
    d.get(0, layer);
    if (layer.size() == 0) return 0;
    std::vector<double> ways(layer.size(), 0), next;
    ways[d.start] = 1;
    for (int len = 0; len < W; len++) {
        next.assign(d.statesAt(len + 1), 0);
        for (size_t v = 0; v < layer.size(); v++)
            if (ways[v] != 0)
                for (uint32_t e = layer.off[v]; e < layer.off[v + 1]; e++) next[layer.tgt[e]] += ways[v];
        ways.swap(next);
        d.get(len + 1, layer);
        if (layer.size() == 0) return 0;
    }
    double total = 0;
    for (size_t v = 0; v < layer.size(); v++) if (layer.acc[v]) total += ways[v];
    return total;
}

bool acceptsFlat(const LayeredDfa& d, int W) {
    Layer layer;
    int32_t q = d.start;
    for (int i = 0; i < W; i++) {
        d.get(i, layer);
        if ((size_t)q >= layer.size()) return false;
        int32_t next = -1;
        for (uint32_t e = layer.off[q]; e < layer.off[q + 1]; e++) if (layer.sym[e] == 0) next = layer.tgt[e];
        if (next < 0) return false;
        q = next;
    }
    d.get(W, layer);
    return (size_t)q < layer.size() && layer.acc[q];
}

// The canonical encoding (BFS renumbering; edges are stored in increasing symbol order), one layer at a time.
// A BFS from the start meets the layers in order, so a state's number is the count of states in the layers
// before its own plus its rank of discovery in its layer.
class CanonicalWalker {
public:
    explicit CanonicalWalker(const LayeredDfa& d) : d(d) {
        d.get(0, layer);
        if (layer.size() > 0) order.push_back(d.start);
    }
    // the next layer's encoding; false once every layer is done
    bool next(std::vector<uint64_t>& out) {
        out.clear();
        if (order.empty()) return false;
        const uint64_t nextBase = base + order.size();
        num.assign(d.statesAt(depth + 1), -1);
        nextOrder.clear();
        for (int32_t v : order) {
            out.push_back(layer.acc[v] ? 0xAULL : 0xBULL);
            for (uint32_t e = layer.off[v]; e < layer.off[v + 1]; e++) {
                int32_t t = layer.tgt[e];
                if (num[t] < 0) {
                    num[t] = (int32_t)nextOrder.size();
                    nextOrder.push_back(t);
                }
                out.push_back((uint64_t)layer.sym[e] << 40 | (nextBase + (uint64_t)num[t]));
            }
            out.push_back(0xFFFFULL);
        }
        order.swap(nextOrder);
        base = nextBase;
        d.get(++depth, layer);
        return true;
    }

private:
    const LayeredDfa& d;
    Layer layer;
    std::vector<int32_t> order, nextOrder, num;
    uint64_t base = 0;
    size_t depth = 0;
};

std::pair<uint64_t, uint64_t> canonicalHash(const LayeredDfa& d) {
    uint64_t h1 = 0x243F6A8885A308D3ULL, h2 = 0x13198A2E03707344ULL;
    CanonicalWalker walk(d);
    std::vector<uint64_t> part;
    while (walk.next(part))
        for (uint64_t v : part) {
            h1 ^= v + 0x9E3779B97F4A7C15ULL + (h1 << 6) + (h1 >> 2);
            h1 *= 0xBF58476D1CE4E5B9ULL;
            h2 += v * 0x94D049BB133111EBULL;
            h2 ^= h2 >> 31;
            h2 *= 0xD6E8FEB86659FD93ULL;
        }
    return {h1, h2};
}

std::vector<uint64_t> canonicalForm(const LayeredDfa& d) {
    std::vector<uint64_t> enc, part;
    CanonicalWalker walk(d);
    while (walk.next(part)) enc.insert(enc.end(), part.begin(), part.end());
    return enc;
}

// Whether two minimal DFAs accept the same words: their canonical encodings compared layer by layer.
bool sameLanguage(const LayeredDfa& a, const LayeredDfa& b) {
    if (a.size() != b.size() || a.edges() != b.edges()) return false;
    CanonicalWalker wa(a), wb(b);
    std::vector<uint64_t> pa, pb;
    while (true) {
        bool ra = wa.next(pa), rb = wb.next(pb);
        if (ra != rb) return false;
        if (!ra) return true;
        if (pa != pb) return false;
    }
}

// --lookahead k: drop frontiers containing a window of WINDOW_COLS columns whose cells of the next k rows
// cannot be covered even with every cell around the window empty (the most room pieces can get), with the
// real wall where the window touches one. Such a frontier has no k-row continuation, so the kept set's
// descendants are exactly the full set's: repeats and rectangles are found as before, and an empty kept
// set means the width dies within k rows (the exact death row is then found by stepping the full set).
// On small widths 7-column windows catch 99-100% of the frontiers that die within 1-3 rows.
constexpr int WINDOW_COLS = 7;
constexpr int WINDOW_MARGIN = 6;  // a piece reaches at most 6 columns from any of its cells

class Lookahead {
public:
    Lookahead(const std::vector<Orientation>& oris, int rows) : rows(rows) {
        for (auto& o : oris) {
            std::vector<Cell> shape;
            for (auto& c : o.cells) shape.push_back(c);
            shapes.push_back(shape);
        }
    }

    int depth() const { return rows; }

    // the frontiers of d with no bad window (d is a layered DFA of width-W words)
    // The product of d with the last w-1 columns read is built one column position at a time (each position's
    // states in parallel) and written to the scratch file, then minimized last layer first, so only one layer
    // of it is ever in memory.
    LayeredDfa keep(LayeredDfa d, int W, ScratchFiles& files, Workers& workers) const {
        if (rows == 0) return d;
        if (W > WINDOW_COLS && filterCovers(d, workers)) return keepFiltered(d, W, files, workers);
        const int w = std::min(WINDOW_COLS, W);
        const uint64_t mask = w > 1 ? (1ULL << (6 * (w - 1))) - 1 : 0;
        DiskSink sink;
        const std::string raw = files.fresh();
        sink.f = fopen(raw.c_str(), "w+b");
        if (!sink.f) fail("cannot open scratch file " + raw);
        Layer in;
        Segmented<WindowKey> layer(std::vector<WindowKey>{{d.start, 0}});
        std::vector<std::unordered_map<uint64_t, bool>> caches(workers.size());  // in front of the shared memo
        for (int pos = 0; pos <= W && !layer.empty(); pos++) {
            d.get(pos, in);
            size_t parts = (layer.size() + KEEP_CHUNK - 1) / KEEP_CHUNK;
            std::vector<LayerOutput> out(parts);
            auto nextKeys = std::make_unique<ShardedIds<WindowKey, WindowKeyHash>>();
            workers.forRange(parts, 1, [&](size_t b, size_t e, int worker) {
                auto& cache = caches[worker];
                for (size_t p = b; p < e; p++) {
                    LayerOutput& o = out[p];
                    for (size_t i = p * KEEP_CHUNK; i < std::min(layer.size(), (p + 1) * KEEP_CHUNK); i++) {
                        const WindowKey& key = layer[i];
                        uint8_t degree = 0;
                        for (uint32_t x = in.off[key.v]; x < in.off[key.v + 1]; x++) {
                            uint64_t window = key.hist << 6 | in.sym[x];
                            // the window of columns pos-w+1..pos, complete once w columns are read
                            if (pos + 1 >= w && bad(window, w, pos + 1 == w, pos + 1 == W, cache)) continue;
                            o.sym.push_back(in.sym[x]);
                            o.tgt.push_back(nextKeys->intern(WindowKey{in.tgt[x], window & mask}));
                            degree++;
                        }
                        o.acc.push_back(in.acc[key.v]);
                        o.degree.push_back(degree);
                    }
                }
            });
            peakLayer = std::max(peakLayer, layer.size());
            layer = nextKeys->finish();
            nextKeys.reset();
            writeLayer(out, layer.offsets, sink, workers);
        }
        return minimizeFromDisk(sink, raw, files.fresh(), workers);
    }

    // keep() through the window filter: the product's second component is the filter's state
    LayeredDfa keepFiltered(const LayeredDfa& d, int W, ScratchFiles& files, Workers& workers) const {
        const Filter& f = *filter;
        DiskSink sink;
        const std::string raw = files.fresh();
        sink.f = fopen(raw.c_str(), "w+b");
        if (!sink.f) fail("cannot open scratch file " + raw);
        Layer in;
        Segmented<WindowKey> layer(std::vector<WindowKey>{{d.start, (uint64_t)f.start}});
        for (int pos = 0; pos <= W && !layer.empty(); pos++) {
            d.get(pos, in);
            size_t parts = (layer.size() + KEEP_CHUNK - 1) / KEEP_CHUNK;
            std::vector<LayerOutput> out(parts);
            auto nextKeys = std::make_unique<ShardedIds<WindowKey, WindowKeyHash>>();
            workers.forRange(parts, 1, [&](size_t b, size_t e) {
                for (size_t p = b; p < e; p++) {
                    LayerOutput& o = out[p];
                    for (size_t i = p * KEEP_CHUNK; i < std::min(layer.size(), (p + 1) * KEEP_CHUNK); i++) {
                        const WindowKey& key = layer[i];
                        const size_t row = (size_t)key.hist * f.m;
                        uint8_t degree = 0;
                        for (uint32_t x = in.off[key.v]; x < in.off[key.v + 1]; x++) {
                            int32_t next = f.next[row + f.index[in.sym[x]]];
                            if (next == f.dead) continue;
                            o.sym.push_back(in.sym[x]);
                            o.tgt.push_back(nextKeys->intern(WindowKey{in.tgt[x], (uint64_t)next}));
                            degree++;
                        }
                        o.acc.push_back(in.acc[key.v] && f.acc[key.hist]);
                        o.degree.push_back(degree);
                    }
                }
            });
            peakLayer = std::max(peakLayer, layer.size());
            layer = nextKeys->finish();
            nextKeys.reset();
            writeLayer(out, layer.offsets, sink, workers);
        }
        return minimizeFromDisk(sink, raw, files.fresh(), workers);
    }

    size_t peak() const { return peakLayer; }  // the largest layer of a window product

    // the filter for building d's next row already pruned, or null (no lookahead, a narrow width, or too many
    // symbols)
    const WindowFilter* onlineFilter(const LayeredDfa& d, int W, Workers& workers) const {
        if (rows == 0 || W <= WINDOW_COLS || !filterCovers(d, workers)) return nullptr;
        return filter.get();
    }

private:
    static constexpr size_t MAX_FILTER_ALPHABET = 12;  // 12^7 windows, three tests each
    static constexpr size_t FILTER_CHUNK = 1 << 14;
    using Filter = WindowFilter;
    mutable std::unique_ptr<Filter> filter;

    // Whether a filter covering d's symbols exists, building (or widening) it when that alphabet is small
    // enough. The kept sets are the same either way; the filter only makes keep() faster and smaller.
    bool filterCovers(const LayeredDfa& d, Workers& workers) const {
        std::array<char, 64> used{};
        for (int s = 0; s < 64; s++) used[s] = (char)(d.symbolsUsed() >> s & 1);
        if (filter) {
            bool covered = true;
            for (int s = 0; s < 64; s++) if (used[s] && filter->index[s] < 0) covered = false;
            if (covered) return true;
            for (int s = 0; s < 64; s++) if (filter->index[s] >= 0) used[s] = 1;
        }
        std::vector<int> alphabet;
        for (int s = 0; s < 64; s++) if (used[s]) alphabet.push_back(s);
        if (alphabet.size() > MAX_FILTER_ALPHABET) return false;
        auto t = std::chrono::steady_clock::now();
        buildFilter(alphabet, workers);
        std::cerr << "    window filter over " << alphabet.size() << " symbols: " << filter->states << " states ("
                  << std::chrono::duration<double>(std::chrono::steady_clock::now() - t).count() << " s)\n"
                  << std::flush;
        return true;
    }

    // The minimal DFA of the words (over `alphabet`) that keep() keeps for widths > WINDOW_COLS: the first
    // complete window is tested with the left wall, later ones without walls, and the last one with the right
    // wall instead (a state accepts when its last window passes that test). Every window over the alphabet
    // is tested once, in parallel; the DFA's states are the prefixes shorter than a window and the last
    // WINDOW_COLS-1 symbols with the result of the right-wall test, minimized by Moore refinement.
    void buildFilter(const std::vector<int>& alphabet, Workers& workers) const {
        const int m = (int)alphabet.size(), w = WINDOW_COLS;
        uint64_t windows = 1;
        for (int i = 0; i < w; i++) windows *= m;
        const uint64_t histories = windows / m;
        constexpr uint8_t BAD_NONE = 1, BAD_LEFT = 2, BAD_RIGHT = 4;
        std::vector<uint8_t> verdict(windows, 0);
        workers.forRange(windows, FILTER_CHUNK, [&](size_t b, size_t e) {
            for (uint64_t i = b; i < e; i++) {
                uint64_t window = 0, rest = i;
                std::array<int, WINDOW_COLS> digit{};
                for (int c = w - 1; c >= 0; c--) { digit[c] = (int)(rest % m); rest /= m; }
                for (int c = 0; c < w; c++) window = window << 6 | (uint64_t)alphabet[digit[c]];
                uint8_t v = 0;
                if (badUncached(window, w, false, false)) v |= BAD_NONE;
                if (badUncached(window, w, true, false)) v |= BAD_LEFT;
                if (badUncached(window, w, false, true)) v |= BAD_RIGHT;
                verdict[i] = v;
            }
        });
        // raw states: prefixes of length 0..w-1, then (history, accepts) pairs, END, DEAD
        std::vector<uint64_t> prefixBase(w);
        uint64_t n = 0;
        for (uint64_t len = 0, size = 1; len < (uint64_t)w; len++, size *= m) { prefixBase[len] = n; n += size; }
        const uint64_t steadyBase = n;
        n += 2 * histories;
        const uint64_t END = n++, DEAD = n++;
        if (n * m >= INT32_MAX) fail("the window filter's alphabet is too large");
        std::vector<int32_t> raw(n * m, (int32_t)DEAD);
        std::vector<uint8_t> rawAcc(n, 0);
        rawAcc[END] = 1;
        for (uint64_t len = 0, size = 1; len + 1 < (uint64_t)w; len++, size *= m)
            for (uint64_t v = 0; v < size; v++)
                for (int a = 0; a < m; a++) raw[(prefixBase[len] + v) * m + a] = (int32_t)(prefixBase[len + 1] + v * m + a);
        for (uint64_t v = 0; v < histories; v++)
            for (int a = 0; a < m; a++) {
                uint64_t win = v * m + a;
                if (!(verdict[win] & BAD_LEFT)) raw[(prefixBase[w - 1] + v) * m + a] = (int32_t)(steadyBase + 2 * (win % histories));
            }
        for (uint64_t h = 0; h < histories; h++)
            for (int acc = 0; acc < 2; acc++) {
                uint64_t s = steadyBase + 2 * h + acc;
                rawAcc[s] = (uint8_t)acc;
                for (int a = 0; a < m; a++) {
                    uint64_t win = h * m + a;
                    bool goodNone = !(verdict[win] & BAD_NONE), goodRight = !(verdict[win] & BAD_RIGHT);
                    uint64_t t = DEAD;
                    if (goodNone) t = steadyBase + 2 * (win % histories) + (goodRight ? 1 : 0);
                    else if (goodRight) t = END;
                    raw[s * m + a] = (int32_t)t;
                }
            }
        std::vector<uint8_t>().swap(verdict);
        // Moore refinement
        std::vector<int32_t> cls(n), nextCls(n);
        for (uint64_t s = 0; s < n; s++) cls[s] = rawAcc[s];
        size_t classes = 2;
        std::vector<int32_t> key(m + 1);
        while (true) {
            std::map<std::vector<int32_t>, int32_t> sig;
            for (uint64_t s = 0; s < n; s++) {
                key[0] = cls[s];
                for (int a = 0; a < m; a++) key[a + 1] = cls[raw[s * m + a]];
                nextCls[s] = sig.try_emplace(key, (int32_t)sig.size()).first->second;
            }
            cls.swap(nextCls);
            if (sig.size() == classes) break;
            classes = sig.size();
        }
        auto f = std::make_unique<Filter>();
        f->index.fill(-1);
        for (int a = 0; a < m; a++) f->index[alphabet[a]] = (int8_t)a;
        f->m = m;
        f->next.assign(classes * m, -1);
        f->acc.assign(classes, 0);
        for (uint64_t s = 0; s < n; s++) {
            f->acc[cls[s]] = rawAcc[s];
            for (int a = 0; a < m; a++) f->next[(size_t)cls[s] * m + a] = cls[raw[s * m + a]];
        }
        f->start = cls[prefixBase[0]];
        f->dead = cls[DEAD];
        f->states = classes;
        filter = std::move(f);
    }

    static constexpr size_t KEEP_CHUNK = 1 << 12;
    static constexpr int MEMO_SHARDS = 64;
    struct WindowKey {
        int32_t v;
        uint64_t hist;
        bool operator==(const WindowKey& o) const { return v == o.v && hist == o.hist; }
    };
    struct WindowKeyHash {
        size_t operator()(const WindowKey& k) const {
            uint64_t h = (uint64_t)(uint32_t)k.v * 0x9E3779B97F4A7C15ULL ^ k.hist * 0xBF58476D1CE4E5B9ULL;
            h ^= h >> 31;
            h *= 0x94D049BB133111EBULL;
            return h ^ (h >> 29);
        }
    };
    struct MemoShard {
        std::mutex m;
        std::unordered_map<uint64_t, bool> verdicts;
    };
    int rows;
    std::vector<std::vector<Cell>> shapes;  // (dr, dc) from the first row-major cell
    mutable std::array<MemoShard, MEMO_SHARDS> memo;
    mutable size_t peakLayer = 0;

    // window: w columns of 6 bits, oldest in the highest bits
    bool bad(uint64_t window, int w, bool leftWall, bool rightWall, std::unordered_map<uint64_t, bool>& cache) const {
        uint64_t key = window << 3 | (uint64_t)leftWall << 1 | (uint64_t)rightWall;
        if (auto it = cache.find(key); it != cache.end()) return it->second;
        MemoShard& shard = memo[(key * 0x9E3779B97F4A7C15ULL) >> 58];
        {
            std::lock_guard<std::mutex> lock(shard.m);
            auto it = shard.verdicts.find(key);
            if (it != shard.verdicts.end()) return cache.emplace(key, it->second).first->second;
        }
        bool result = badUncached(window, w, leftWall, rightWall);
        cache.emplace(key, result);
        std::lock_guard<std::mutex> lock(shard.m);
        shard.verdicts.emplace(key, result);
        return result;
    }

    bool badUncached(uint64_t window, int w, bool leftWall, bool rightWall) const {
        int left = leftWall ? 0 : WINDOW_MARGIN, right = rightWall ? 0 : WINDOW_MARGIN;
        int width = left + w + right;
        std::vector<uint32_t> cols(width, 0);
        for (int c = 0; c < w; c++) cols[left + c] = (window >> (6 * (w - 1 - c))) & 63;
        const int height = 6 + rows;  // cells above the k rows can hold the pieces that cover them
        std::function<bool()> fill = [&]() -> bool {
            int r0 = -1, c0 = -1;
            for (int r = 0; r < rows && r0 < 0; r++)
                for (int c = left; c < left + w; c++)
                    if (!(cols[c] >> r & 1)) { r0 = r; c0 = c; break; }
            if (r0 < 0) return true;
            for (auto& shape : shapes) {
                for (auto& anchor : shape) {
                    bool ok = true;
                    for (auto& cell : shape) {
                        int r = r0 + cell.first - anchor.first, c = c0 + cell.second - anchor.second;
                        if (r < 0 || r >= height || c < 0 || c >= width || (cols[c] >> r & 1)) { ok = false; break; }
                    }
                    if (!ok) continue;
                    for (auto& cell : shape) cols[c0 + cell.second - anchor.second] |= 1u << (r0 + cell.first - anchor.first);
                    bool done = fill();
                    for (auto& cell : shape) cols[c0 + cell.second - anchor.second] &= ~(1u << (r0 + cell.first - anchor.first));
                    if (done) return true;
                }
            }
            return false;
        };
        return !fill();
    }
};

struct SeenRow { int32_t row; uint64_t h1, h2; };

// Everything needed to continue a run: row `row`'s language, the hashes and state counts of the rows before.
struct Checkpoint {
    int32_t width = 0, startRow = 0, row = 0;
    double seconds = 0;
    std::string pieceKey;
    std::vector<SeenRow> seen;
    std::vector<uint64_t> counts;
    LayeredDfa lang;
};

// n, e (uint64), start (int32), acc[n], degree[n] (uint8), sym[e], tgt[e] (int32); edges in state order, the
// states numbered layer by layer. Written from one pass over the layers, each layer's four parts in place.
void writeLanguage(FILE* f, const LayeredDfa& d) {
    const uint64_t n = d.size(), e = d.edges();
    if (n >= INT32_MAX) fail("the automaton is too large for 32-bit ids");
    writeValue(f, n, "language");
    writeValue(f, e, "language");
    writeValue(f, d.start, "language");
    const int64_t accAt = FTELL(f), degreeAt = accAt + (int64_t)n, symAt = degreeAt + (int64_t)n,
                  tgtAt = symAt + (int64_t)e;
    Layer l;
    std::vector<uint8_t> degree;
    std::vector<int32_t> global;
    uint64_t base = 0, edgeBase = 0;
    auto at = [&](int64_t pos) { if (FSEEK(f, pos, SEEK_SET) != 0) fail("seek failed while writing an automaton"); };
    for (size_t k = 0; k < d.depth(); k++) {
        d.get(k, l);
        const uint64_t nextBase = base + l.size();
        degree.resize(l.size());
        for (size_t v = 0; v < l.size(); v++) degree[v] = (uint8_t)(l.off[v + 1] - l.off[v]);  // at most SYMBOLS
        global.resize(l.tgt.size());
        for (size_t x = 0; x < l.tgt.size(); x++) global[x] = (int32_t)(nextBase + (uint64_t)l.tgt[x]);
        at(accAt + (int64_t)base);
        writeAll(f, l.acc.data(), l.size(), "language");
        at(degreeAt + (int64_t)base);
        writeAll(f, degree.data(), l.size(), "language");
        at(symAt + (int64_t)edgeBase);
        writeAll(f, l.sym.data(), l.sym.size(), "language");
        at(tgtAt + 4 * (int64_t)edgeBase);
        writeAll(f, global.data(), 4 * global.size(), "language");
        base = nextBase;
        edgeBase += l.sym.size();
    }
    at(tgtAt + 4 * (int64_t)e);
}

// --dump: one file per row, for looking at the automata (research/automaton_view.py reads them)
void dumpLanguage(const LayeredDfa& d, const std::string& dir, int row) {
    std::string path = dir + "/row" + std::to_string(row) + ".dfa";
    FILE* f = fopen(path.c_str(), "wb");
    if (!f) fail("cannot write " + path);
    writeLanguage(f, d);
    if (fclose(f) != 0) fail("cannot finish " + path + "; is the disk full?");
}

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
    writeLanguage(f, c.lang);
    writeAll(f, CHECKPOINT_END, sizeof CHECKPOINT_END, "checkpoint");
    if (fflush(f) != 0 || fclose(f) != 0) fail("cannot finish checkpoint " + tmp + "; is the disk full?");
    std::error_code error;
    std::filesystem::rename(tmp, path, error);
    if (error) fail("cannot replace checkpoint " + path + ": " + error.message());
}

// The checkpoint's automaton is read one layer at a time into a LayeredDfa: its states are numbered layer by
// layer (layer 0 is the start state), so each layer is the range of its predecessor layer's targets.
Checkpoint loadCheckpoint(const std::string& path, ScratchFiles& files) {
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
    const uint64_t n = readValue<uint64_t>(f, "checkpoint"), e = readValue<uint64_t>(f, "checkpoint");
    const int32_t start = readValue<int32_t>(f, "checkpoint");
    if (n == 0 || start != 0) fail(path + " is damaged (start state)");
    const int64_t accAt = FTELL(f), degreeAt = accAt + (int64_t)n, symAt = degreeAt + (int64_t)n,
                  tgtAt = symAt + (int64_t)e, endAt = tgtAt + 4 * (int64_t)e;
    auto at = [&](int64_t pos) { if (FSEEK(f, pos, SEEK_SET) != 0) fail(path + " is damaged (too short)"); };
    LayeredDfa& d = c.lang;
    d = LayeredDfa(files.fresh());
    Layer l;
    std::vector<uint8_t> degree;
    uint64_t lo = 0, hi = 1, edgeLo = 0;
    for (size_t k = 0; lo < hi; k++) {
        const uint64_t count = hi - lo;
        degree.resize(count);
        at(degreeAt + (int64_t)lo);
        readAll(f, degree.data(), count, "checkpoint");
        l.off.assign(count + 1, 0);
        for (uint64_t v = 0; v < count; v++) l.off[v + 1] = l.off[v] + degree[v];
        const uint64_t layerEdges = l.off[count];
        if (edgeLo + layerEdges > e) fail(path + " is damaged (edge count)");
        l.acc.resize(count);
        at(accAt + (int64_t)lo);
        readAll(f, l.acc.data(), count, "checkpoint");
        l.sym.resize(layerEdges);
        l.tgt.resize(layerEdges);
        at(symAt + (int64_t)edgeLo);
        readAll(f, l.sym.data(), layerEdges, "checkpoint");
        at(tgtAt + 4 * (int64_t)edgeLo);
        readAll(f, l.tgt.data(), 4 * layerEdges, "checkpoint");
        uint64_t nextHi = hi;
        for (int32_t& t : l.tgt) {
            if (t < 0 || (uint64_t)t < hi || (uint64_t)t >= n) fail(path + " is damaged (edge target)");
            nextHi = std::max(nextHi, (uint64_t)t + 1);
            t -= (int32_t)hi;
        }
        d.put(k, l);
        edgeLo += layerEdges;
        lo = hi;
        hi = nextHi;
    }
    if (lo != n || edgeLo != e) fail(path + " is damaged (states outside the layers)");
    at(endAt);
    char end[sizeof CHECKPOINT_END];
    readAll(f, end, sizeof end, "checkpoint");
    if (memcmp(end, CHECKPOINT_END, sizeof end) != 0) fail(path + " is damaged (no end mark)");
    fclose(f);
    d.start = 0;
    return c;
}

struct Options {
    int width = 0;
    int startRow = DEFAULT_START_ROW;
    std::vector<Cell> cells;
    std::string cellsText, checkpoint, scratch, dumpDir;
    int lookahead = 0;
    int threads = 1;
};

const char* USAGE =
    "usage: exact_width --width W --cells \"x,y x,y ...\" --scratch FILE [--checkpoint FILE] [--start-row R]\n"
    "                   [--dump DIR] [--lookahead K] [--threads N]\n"
    "Decides exactly whether the piece's width-W half-strip dies, tiles a rectangle or fills forever.\n"
    "With --checkpoint, resumes from FILE if it exists and rewrites it after every row. The scratch file\n"
    "holds one raw row. --dump writes every row's minimal automaton to DIR/row<r>.dfa. --lookahead K keeps only\n"
    "frontiers that can fill K more rows (same verdicts; the state counts are of the kept sets). --threads N\n"
    "spreads each row's work over N threads (same verdicts and state counts).\n";

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
        else if (a == "--dump") o.dumpDir = v;
        else if (a == "--lookahead") o.lookahead = parseInt(v, "--lookahead");
        else if (a == "--threads") o.threads = parseInt(v, "--threads");
        else fail("unknown option " + a + "\n" + USAGE);
    }
    if (o.width < 1 || o.width > 2000) fail("--width must be between 1 and 2000");
    if (o.startRow < 0 || o.startRow > 12) fail("--start-row must be between 0 and 12");
    if (o.lookahead < 0 || o.lookahead > 6) fail("--lookahead must be between 0 and 6");
    if (o.threads < 1 || o.threads > MAX_THREADS) fail("--threads must be between 1 and " + std::to_string(MAX_THREADS));
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
std::pair<int, LayeredDfa> firstRow(const std::vector<Orientation>& oris, int startRow, int W, ScratchFiles& files) {
    Dfa lang;
    std::array<int32_t, SYMBOLS> row;
    row.fill(-1);
    lang.next = {row, row};
    lang.next[0][0] = 1;
    lang.next[1][0] = 1;
    lang.accept = {0, 1};
    for (int r = 0;; r++) {
        LayeredDfa d = toLayered(minimizeAcyclic(restrictLength(lang, W)), files.fresh());
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

// Thrown to end a run from inside a nested step; main returns its code once the stack (and with it every
// automaton file) is unwound. std::exit would leave the files open, and Windows cannot delete open files.
struct Finished { int code; };

int run(int argc, char** argv) {
    Options opt = parseOptions(argc, argv);
    auto oris = orientationsOf(opt.cells);
    const int W = opt.width;
    Workers workers(opt.threads);
    // the row automata live in <scratch>.<n> files, each deleted with its object (see Finished)
    ScratchFiles files(opt.scratch);
    files.removeLeftovers();
    auto t0 = std::chrono::steady_clock::now();
    auto elapsed = [&]() { return std::chrono::duration<double>(std::chrono::steady_clock::now() - t0).count(); };

    // Runs with different lookaheads keep different sets, so a checkpoint records its lookahead in the piece
    // key, and a resumed run goes on with the checkpoint's lookahead whatever --lookahead says.
    const std::string baseKey = pieceKeyOf(oris);
    const std::string lookaheadMark = "lookahead=";
    Checkpoint c;
    const bool keepCheckpoints = !opt.checkpoint.empty();
    const bool resuming = keepCheckpoints && std::filesystem::exists(opt.checkpoint);
    int lookahead = opt.lookahead;
    if (resuming) {
        c = loadCheckpoint(opt.checkpoint, files);
        bool samePiece = c.pieceKey.compare(0, baseKey.size(), baseKey) == 0;
        std::string rest = samePiece ? c.pieceKey.substr(baseKey.size()) : "";
        if (c.width != W || c.startRow != opt.startRow || !samePiece ||
            !(rest.empty() || rest.compare(0, lookaheadMark.size(), lookaheadMark) == 0))
            fail(opt.checkpoint + " belongs to another width or piece");
        lookahead = rest.empty() ? 0 : parseInt(rest.substr(lookaheadMark.size()), "the checkpoint's lookahead");
    }
    const Lookahead look(oris, lookahead);
    const std::string key = baseKey + (lookahead ? lookaheadMark + std::to_string(lookahead) : "");
    double before = 0;  // compute seconds of earlier runs (from the checkpoint)
    std::map<int32_t, LayeredDfa> recent;  // small kept rows of the last `lookahead` rows
    // Row `row`'s kept set is empty: the width dies, and this finds the first row with no filling at all. A
    // frontier pruned at row r has fillings up to row r + lookahead - 1 at most, so the unpruned language
    // stepped from the kept row row - lookahead (or from the first row) reaches every filling that can outlive
    // `row`. That kept row is in `recent`, or rebuilt from the start. Rows after `row` print as empty kept sets.
    auto deathAt = [&](int row) {
        int from = row - lookahead;
        LayeredDfa full;
        int at;
        if (from < c.startRow) {
            auto [r0, first] = firstRow(oris, opt.startRow, W, files);
            full = std::move(first);
            at = r0;
        } else {
            LayeredDfa e;
            if (auto it = recent.find(from); it != recent.end()) {
                e = std::move(it->second);
            } else {
                std::cerr << "    rebuilding the kept row " << from << " for the exact death row\n" << std::flush;
                auto [r0, first] = firstRow(oris, opt.startRow, W, files);
                e = look.keep(std::move(first), W, files, workers);
                for (int r = r0; r < from; r++) e = look.keep(advanceRow(oris, e, files, workers), W, files, workers);
            }
            full = advanceRow(oris, e, files, workers);
            at = from + 1;
        }
        while (countWords(full, W) > 0) {
            full = advanceRow(oris, full, files, workers);
            at++;
        }
        for (int r = row + 1; r <= at; r++) {
            c.counts.push_back(1);
            std::cout << "row " << r << " states 1 words 0 seconds " << (int64_t)(before + elapsed()) << "\n";
        }
        printCounts(c.counts);
        std::cout << "dead " << std::max(at, row) << "\n" << std::flush;
        throw Finished{0};
    };
    // the kept part of a row's full set; an empty kept part ends the run with the exact death row
    auto kept = [&](LayeredDfa full, int row) -> LayeredDfa {
        if (look.depth() == 0 || countWords(full, W) == 0) return full;
        auto t = std::chrono::steady_clock::now();
        const uint64_t fullStates = full.size();
        LayeredDfa k = look.keep(std::move(full), W, files, workers);
        std::cerr << "    kept " << k.size() << " of " << fullStates << " states (largest window layer " << look.peak()
                  << ", " << std::chrono::duration<double>(std::chrono::steady_clock::now() - t).count() << " s)\n" << std::flush;
        if (countWords(k, W) > 0) return k;
        c.counts.push_back(1);
        std::cout << "row " << row << " states 1 words 0 seconds " << (int64_t)(before + elapsed()) << "\n";
        k.reset();
        deathAt(row);
        return k;
    };
    // the next row's kept set: pruned while it is built when the window filter applies; otherwise, or when
    // nothing is kept or a symbol is outside the filter's alphabet, built in full and pruned afterwards
    auto keptNext = [&](LayeredDfa& d, int row) -> LayeredDfa {
        if (const WindowFilter* f = look.onlineFilter(d, W, workers)) {
            bool uncovered = false;
            LayeredDfa k = advanceRow(oris, d, files, workers, f, &uncovered);
            if (!uncovered && countWords(k, W) > 0) {
                d.reset();
                std::cerr << "    kept " << k.size() << " states (pruned while built)\n" << std::flush;
                return k;
            }
        }
        return kept(advanceRow(oris, d, files, workers), row);
    };
    std::cout << "lookahead " << lookahead << "\n";
    if (resuming) {
        before = c.seconds;
        std::cout << "start " << c.row << "\n" << std::flush;
    } else {
        c.width = W;
        c.startRow = opt.startRow;
        c.pieceKey = key;
        auto [row, full] = firstRow(oris, opt.startRow, W, files);
        c.row = row;
        std::cout << "start " << c.row << "\n" << std::flush;
        c.lang = kept(std::move(full), row);
    }
    std::map<std::pair<uint64_t, uint64_t>, int32_t> seen;
    for (auto& s : c.seen) seen.emplace(std::make_pair(s.h1, s.h2), s.row);
    std::map<int32_t, std::vector<uint64_t>> smallForms;  // rows of this run, not those of a resumed checkpoint

    while (true) {
        LayeredDfa& d = c.lang;
        if (!opt.dumpDir.empty()) dumpLanguage(d, opt.dumpDir, c.row);
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
            bool same;
            if (auto stored = smallForms.find(earlier); stored != smallForms.end()) {
                same = d.size() <= SMALL_ROW_STATES && stored->second == canonicalForm(d);
            } else {
                auto [r0, first] = firstRow(oris, opt.startRow, W, files);
                LayeredDfa e = look.keep(std::move(first), W, files, workers);
                for (int r = r0; r < earlier; r++) e = look.keep(advanceRow(oris, e, files, workers), W, files, workers);
                same = sameLanguage(e, d);
            }
            if (same) {
                printCounts(c.counts);
                std::cout << "forever " << earlier << " " << c.row << "\n" << std::flush;
                return 0;
            }
            std::cerr << "hash collision between rows " << earlier << " and " << c.row << "\n" << std::flush;
        }
        seen.emplace(h, c.row);
        c.seen.push_back({c.row, h.first, h.second});
        if (d.size() <= SMALL_ROW_STATES) smallForms.emplace(c.row, canonicalForm(d));
        if (lookahead > 0) {
            recent.erase(recent.begin(), recent.lower_bound(c.row - lookahead + 1));
            if (d.size() <= RECENT_ROW_STATES) recent.emplace(c.row, d.copy(files.fresh()));
        }
        c.lang = keptNext(d, c.row + 1);
        c.row++;
        c.seconds = before + elapsed();
        if (keepCheckpoints) saveCheckpoint(c, opt.checkpoint);
    }
}

int main(int argc, char** argv) {
    try {
        return run(argc, argv);
    } catch (const Finished& finished) {
        return finished.code;
    }
}
