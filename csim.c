#include <getopt.h>
#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>
#include "cachelab.h"

typedef struct {
    int valid;
    int dirty;
    unsigned long long tag;
    unsigned long long lru;
} line_t;

typedef struct {
    line_t *lines;
    int mru;
} set_t;

static int s, E, b;
static int verbose = 0;
static set_t *cache;
static unsigned long long timer = 0;

static int hit_count = 0;
static int miss_count = 0;
static int eviction_count = 0;
static int dirty_bytes_evicted = 0;
static int double_refs = 0;

static void printUsage(char *prog)
{
    printf("Usage: %s [-hv] -s <s> -E <E> -b <b> -t <tracefile>\n", prog);
    printf("  -h  Print this help message.\n");
    printf("  -v  Optional verbose flag.\n");
    printf("  -s  Number of set index bits.\n");
    printf("  -E  Number of lines per set.\n");
    printf("  -b  Number of block offset bits.\n");
    printf("  -t  Trace file.\n");
}

static void initCache(void)
{
    int S = 1 << s;
    int i;

    cache = malloc(S * sizeof(set_t));
    if (cache == NULL) {
        fprintf(stderr, "malloc failed\n");
        exit(1);
    }
    for (i = 0; i < S; i++) {
        cache[i].lines = calloc(E, sizeof(line_t));
        if (cache[i].lines == NULL) {
            fprintf(stderr, "calloc failed\n");
            exit(1);
        }
        cache[i].mru = -1;
    }
}

static void freeCache(void)
{
    int S = 1 << s;
    int i;

    for (i = 0; i < S; i++) {
        free(cache[i].lines);
    }
    free(cache);
}

static void accessCache(unsigned long long addr, int is_store)
{
    unsigned long long set_index = (addr >> b) & ((1ULL << s) - 1);
    unsigned long long tag = addr >> (s + b);
    set_t *set = &cache[set_index];
    int i;
    int victim = -1;

    timer++;

    for (i = 0; i < E; i++) {
        if (set->lines[i].valid && set->lines[i].tag == tag) {
            hit_count++;
            if (set->mru == i) {
                double_refs++;
                if (verbose) printf(" hit-double_ref");
            } else if (verbose) {
                printf(" hit");
            }
            set->lines[i].lru = timer;
            if (is_store) set->lines[i].dirty = 1;
            set->mru = i;
            return;
        }
    }

    miss_count++;
    if (verbose) printf(is_store ? " dirty-miss" : " miss");

    for (i = 0; i < E; i++) {
        if (!set->lines[i].valid) {
            victim = i;
            break;
        }
    }
    if (victim == -1) {
        victim = 0;
        for (i = 1; i < E; i++) {
            if (set->lines[i].lru < set->lines[victim].lru) {
                victim = i;
            }
        }
        eviction_count++;
        if (set->lines[victim].dirty) {
            dirty_bytes_evicted += (1 << b);
            if (verbose) printf(" dirty-eviction");
        } else if (verbose) {
            printf(" eviction");
        }
    }

    set->lines[victim].valid = 1;
    set->lines[victim].tag = tag;
    set->lines[victim].dirty = is_store;
    set->lines[victim].lru = timer;
    set->mru = victim;
}

static int countDirtyActive(void)
{
    int S = 1 << s;
    int i, j;
    int total = 0;

    for (i = 0; i < S; i++) {
        for (j = 0; j < E; j++) {
            if (cache[i].lines[j].valid && cache[i].lines[j].dirty) {
                total += (1 << b);
            }
        }
    }
    return total;
}

int main(int argc, char *argv[])
{
    char *trace_file = NULL;
    FILE *fp;
    char op;
    unsigned long long addr;
    int size;
    int opt;

    s = E = b = -1;
    while ((opt = getopt(argc, argv, "hvs:E:b:t:")) != -1) {
        switch (opt) {
        case 'h':
            printUsage(argv[0]);
            return 0;
        case 'v':
            verbose = 1;
            break;
        case 's':
            s = atoi(optarg);
            break;
        case 'E':
            E = atoi(optarg);
            break;
        case 'b':
            b = atoi(optarg);
            break;
        case 't':
            trace_file = optarg;
            break;
        default:
            printUsage(argv[0]);
            return 1;
        }
    }

    if (s < 0 || E <= 0 || b < 0 || trace_file == NULL) {
        fprintf(stderr, "%s: Missing required command line argument\n",
                argv[0]);
        printUsage(argv[0]);
        return 1;
    }

    fp = fopen(trace_file, "r");
    if (fp == NULL) {
        fprintf(stderr, "%s: No such file or directory\n", trace_file);
        return 1;
    }

    initCache();

    while (fscanf(fp, " %c %llx,%d", &op, &addr, &size) == 3) {
        if (op == 'I') continue;
        if (verbose) printf("%c %llx,%d", op, addr, size);
        switch (op) {
        case 'L':
            accessCache(addr, 0);
            break;
        case 'S':
            accessCache(addr, 1);
            break;
        case 'M':
            accessCache(addr, 0);
            accessCache(addr, 1);
            break;
        default:
            break;
        }
        if (verbose) printf("\n");
    }
    fclose(fp);

    printSummary(hit_count, miss_count, eviction_count,
                 dirty_bytes_evicted, countDirtyActive(), double_refs);

    freeCache();
    return 0;
}
