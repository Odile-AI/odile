/* bench-snpreport — the QEMU-TCG stand-in for Servury's snpreport (ODL-454).
 *
 * The bench host has no /dev/kvm and no AMD PSP, so SNP_GET_DERIVED_KEY cannot
 * run here. This binary keeps the *interface and the file format* the
 * bb-sealed-unlock hook and bb-attest use, and replaces only the source of the
 * 32-byte key:
 *
 *   real Blackbox   K_t = SNP_GET_DERIVED_KEY(VCEK, policy|measurement)
 *   this bench      K_b = HMAC-SHA-256(BENCH_VCEK, "odile-bench-snp-v1\n" ||
 *                                     /proc/cmdline)
 *
 * The measured command line is the one leg of the real measurement that the
 * bench can vary (a changed seal_id, bb.host or bb.iid must change the key;
 * ODL-310 §2/§5 observed exactly that on a real chip). The benchmark root key
 * is a public constant, not a secret: on a real guest the root key is the
 * chip's VCEK, whose private half is AMD's. The boot chain, the hook's
 * placement, the LUKS open and the fail-closed behaviour are what this proves;
 * the real derivation was proven on the tenant in ODL-310.
 *
 * The sealed-file format is Servury's BBHK1, byte for byte:
 *     file = "BBHK1\n" | nonce[16] | ciphertext | tag[32]
 *     ct   = plaintext XOR HMAC-SHA-256(E, nonce | be32(i)), i = 0, 1, ...
 *     tag  = HMAC-SHA-256(M, magic | nonce | ciphertext)
 * with E = HMAC-SHA-256(K, "blackbox hostkey v1 enc") and
 *      M = HMAC-SHA-256(K, "blackbox hostkey v1 mac").
 * spike/sealed-unlock/seal_ref.py unseals what this writes, and vice versa;
 * tests/test_sealed_image.py asserts that both ways.
 *
 *   snpreport --selftest
 *   snpreport --key-hex [--cmdline-file F] [--seed-file F]
 *   snpreport --key-id  [--cmdline-file F] [--seed-file F]
 *   snpreport --fp FILE
 *   snpreport --seal   IN OUT [--cmdline-file F] [--seed-file F]
 *   snpreport --unseal IN OUT [--cmdline-file F] [--seed-file F]
 *
 * Exit codes: 0 ok, 1 I/O or a bad file, 2 usage, 6 the blob is not sealed to
 * this cmdline (the code Servury's snpreport returns; the hook treats any
 * non-zero as fail-closed).
 *
 * Build: gcc -O2 -static -s -o bench-snpreport deploy/image/bench-snpreport.c
 * No library beyond libc; SHA-256 and HMAC are implemented here.
 */
#include <errno.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

/* The public bench root key: 32 bytes of ASCII, literally not a secret. */
static const char BENCH_VCEK_STR[] = "odile-bench-vcek-NOT-a-secret-32";
#define BENCH_VCEK ((const unsigned char *)BENCH_VCEK_STR)
#define BENCH_VCEK_LEN 32

#define MAGIC "BBHK1\n"
#define MAGIC_LEN 6
#define NONCE_LEN 16
#define TAG_LEN 32
#define MAX_PLAIN 4096

/* ------------------------------------------------------------------ SHA-256 */

typedef struct {
    uint32_t h[8];
    uint64_t len;
    unsigned char buf[64];
    size_t used;
} sha256_ctx;

static const uint32_t K256[64] = {
    0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1,
    0x923f82a4, 0xab1c5ed5, 0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3,
    0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174, 0xe49b69c1, 0xefbe4786,
    0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
    0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147,
    0x06ca6351, 0x14292967, 0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13,
    0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85, 0xa2bfe8a1, 0xa81a664b,
    0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
    0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a,
    0x5b9cca4f, 0x682e6ff3, 0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208,
    0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
};

static uint32_t ror32(uint32_t x, int n) { return (x >> n) | (x << (32 - n)); }

static void sha256_compress(sha256_ctx *c, const unsigned char *p) {
    uint32_t w[64], a, b, cc, d, e, f, g, h;
    int i;
    for (i = 0; i < 16; i++)
        w[i] = ((uint32_t)p[4 * i] << 24) | ((uint32_t)p[4 * i + 1] << 16) |
               ((uint32_t)p[4 * i + 2] << 8) | (uint32_t)p[4 * i + 3];
    for (i = 16; i < 64; i++) {
        uint32_t s0 = ror32(w[i - 15], 7) ^ ror32(w[i - 15], 18) ^ (w[i - 15] >> 3);
        uint32_t s1 = ror32(w[i - 2], 17) ^ ror32(w[i - 2], 19) ^ (w[i - 2] >> 10);
        w[i] = w[i - 16] + s0 + w[i - 7] + s1;
    }
    a = c->h[0]; b = c->h[1]; cc = c->h[2]; d = c->h[3];
    e = c->h[4]; f = c->h[5]; g = c->h[6]; h = c->h[7];
    for (i = 0; i < 64; i++) {
        uint32_t S1 = ror32(e, 6) ^ ror32(e, 11) ^ ror32(e, 25);
        uint32_t ch = (e & f) ^ (~e & g);
        uint32_t t1 = h + S1 + ch + K256[i] + w[i];
        uint32_t S0 = ror32(a, 2) ^ ror32(a, 13) ^ ror32(a, 22);
        uint32_t maj = (a & b) ^ (a & cc) ^ (b & cc);
        uint32_t t2 = S0 + maj;
        h = g; g = f; f = e; e = d + t1;
        d = cc; cc = b; b = a; a = t1 + t2;
    }
    c->h[0] += a; c->h[1] += b; c->h[2] += cc; c->h[3] += d;
    c->h[4] += e; c->h[5] += f; c->h[6] += g; c->h[7] += h;
}

static void sha256_init(sha256_ctx *c) {
    c->h[0] = 0x6a09e667; c->h[1] = 0xbb67ae85; c->h[2] = 0x3c6ef372;
    c->h[3] = 0xa54ff53a; c->h[4] = 0x510e527f; c->h[5] = 0x9b05688c;
    c->h[6] = 0x1f83d9ab; c->h[7] = 0x5be0cd19;
    c->len = 0; c->used = 0;
}

static void sha256_update(sha256_ctx *c, const void *data, size_t n) {
    const unsigned char *p = data;
    c->len += (uint64_t)n * 8;
    while (n > 0) {
        size_t take = 64 - c->used;
        if (take > n) take = n;
        memcpy(c->buf + c->used, p, take);
        c->used += take; p += take; n -= take;
        if (c->used == 64) { sha256_compress(c, c->buf); c->used = 0; }
    }
}

static void sha256_final(sha256_ctx *c, unsigned char out[32]) {
    uint64_t bits = c->len;
    unsigned char pad = 0x80;
    sha256_update(c, &pad, 1);
    pad = 0;
    while (c->used != 56) sha256_update(c, &pad, 1);
    for (int i = 7; i >= 0; i--) {
        unsigned char byte = (unsigned char)(bits >> (8 * i));
        sha256_update(c, &byte, 1);
    }
    for (int i = 0; i < 8; i++) {
        out[4 * i] = (unsigned char)(c->h[i] >> 24);
        out[4 * i + 1] = (unsigned char)(c->h[i] >> 16);
        out[4 * i + 2] = (unsigned char)(c->h[i] >> 8);
        out[4 * i + 3] = (unsigned char)c->h[i];
    }
}

static void hmac_sha256(const unsigned char *key, size_t keylen,
                        const void *msg, size_t msglen, unsigned char out[32]) {
    unsigned char block[64], ipad[64], opad[64], inner[32];
    sha256_ctx c;
    memset(block, 0, sizeof block);
    if (keylen > 64) {
        sha256_init(&c);
        sha256_update(&c, key, keylen);
        sha256_final(&c, block);
    } else {
        memcpy(block, key, keylen);
    }
    for (int i = 0; i < 64; i++) { ipad[i] = block[i] ^ 0x36; opad[i] = block[i] ^ 0x5c; }
    sha256_init(&c); sha256_update(&c, ipad, 64); sha256_update(&c, msg, msglen);
    sha256_final(&c, inner);
    sha256_init(&c); sha256_update(&c, opad, 64); sha256_update(&c, inner, 32);
    sha256_final(&c, out);
}

/* ------------------------------------------------------------------- helpers */

static int read_file(const char *path, unsigned char *buf, size_t cap, size_t *len) {
    FILE *fh = fopen(path, "rb");
    if (!fh) return -1;
    *len = fread(buf, 1, cap, fh);
    int extra = fgetc(fh);
    fclose(fh);
    if (extra != EOF) { errno = EFBIG; return -2; }
    return 0;
}

static int write_file(const char *path, const unsigned char *buf, size_t len) {
    FILE *fh = fopen(path, "wb");
    if (!fh) return -1;
    if (fwrite(buf, 1, len, fh) != len) { fclose(fh); return -1; }
    return fclose(fh) == 0 ? 0 : -1;
}

/* The cmdline file's trailing newlines are not part of the measured line. */
static int read_cmdline(const char *path, char *buf, size_t cap) {
    size_t len = 0;
    if (read_file(path, (unsigned char *)buf, cap - 1, &len) != 0) return -1;
    buf[len] = '\0';
    while (len > 0 && (buf[len - 1] == '\n' || buf[len - 1] == '\r')) buf[--len] = '\0';
    return 0;
}

static int read_seed(const char *path, unsigned char seed[32]) {
    if (!path) { memcpy(seed, BENCH_VCEK, BENCH_VCEK_LEN); return 0; }
    size_t len = 0;
    if (read_file(path, seed, 32, &len) != 0 || len != 32) return -1;
    return 0;
}

static void derive_key(const unsigned char seed[32], const char *cmdline,
                       unsigned char key[32]) {
    static const char prefix[] = "odile-bench-snp-v1\n";
    size_t plen = sizeof prefix - 1, clen = strlen(cmdline);
    unsigned char *msg = malloc(plen + clen);
    if (!msg) { perror("malloc"); exit(1); }
    memcpy(msg, prefix, plen);
    memcpy(msg + plen, cmdline, clen);
    hmac_sha256(seed, 32, msg, plen + clen, key);
    memset(msg, 0, plen + clen);
    free(msg);
}

static void hex(const unsigned char *in, size_t n, char *out) {
    static const char digits[] = "0123456789abcdef";
    for (size_t i = 0; i < n; i++) {
        out[2 * i] = digits[in[i] >> 4];
        out[2 * i + 1] = digits[in[i] & 15];
    }
    out[2 * n] = '\0';
}

static void base64_nopad(const unsigned char *in, size_t n, char *out) {
    static const char a[] = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    size_t i, o = 0;
    for (i = 0; i + 2 < n; i += 3) {
        out[o++] = a[in[i] >> 2];
        out[o++] = a[((in[i] & 3) << 4) | (in[i + 1] >> 4)];
        out[o++] = a[((in[i + 1] & 15) << 2) | (in[i + 2] >> 6)];
        out[o++] = a[in[i + 2] & 63];
    }
    if (i + 1 == n) {
        out[o++] = a[in[i] >> 2];
        out[o++] = a[(in[i] & 3) << 4];
    } else if (i + 2 == n) {
        out[o++] = a[in[i] >> 2];
        out[o++] = a[((in[i] & 3) << 4) | (in[i + 1] >> 4)];
        out[o++] = a[(in[i + 1] & 15) << 2];
    }
    out[o] = '\0';
}

/* --------------------------------------------------------------- BBHK1 codec */

static void derive_enc_mac(const unsigned char key[32], unsigned char enc[32],
                           unsigned char mac[32]) {
    static const char ENC[] = "blackbox hostkey v1 enc";
    static const char MAC[] = "blackbox hostkey v1 mac";
    hmac_sha256(key, 32, ENC, sizeof ENC - 1, enc);
    hmac_sha256(key, 32, MAC, sizeof MAC - 1, mac);
}

static void keystream_xor(const unsigned char enc[32], const unsigned char nonce[NONCE_LEN],
                          const unsigned char *in, unsigned char *out, size_t n) {
    for (size_t off = 0; off < n; off += 32) {
        unsigned char counter[4] = {
            (unsigned char)((off / 32) >> 24), (unsigned char)((off / 32) >> 16),
            (unsigned char)((off / 32) >> 8), (unsigned char)(off / 32)};
        unsigned char msg[NONCE_LEN + 4], block[32];
        memcpy(msg, nonce, NONCE_LEN);
        memcpy(msg + NONCE_LEN, counter, 4);
        hmac_sha256(enc, 32, msg, sizeof msg, block);
        size_t take = n - off < 32 ? n - off : 32;
        for (size_t j = 0; j < take; j++) out[off + j] = in[off + j] ^ block[j];
    }
}

static int seal_blob(const unsigned char key[32], const unsigned char *plain,
                     size_t plen, unsigned char *out, size_t *outlen) {
    unsigned char enc[32], mac[32], nonce[NONCE_LEN];
    if (plen > MAX_PLAIN) return -2;
    FILE *r = fopen("/dev/urandom", "rb");
    if (!r || fread(nonce, 1, NONCE_LEN, r) != NONCE_LEN) { if (r) fclose(r); return -1; }
    fclose(r);
    derive_enc_mac(key, enc, mac);
    memcpy(out, MAGIC, MAGIC_LEN);
    memcpy(out + MAGIC_LEN, nonce, NONCE_LEN);
    keystream_xor(enc, nonce, plain, out + MAGIC_LEN + NONCE_LEN, plen);
    *outlen = MAGIC_LEN + NONCE_LEN + plen + TAG_LEN;
    hmac_sha256(mac, 32, out, MAGIC_LEN + NONCE_LEN + plen, out + *outlen - TAG_LEN);
    return 0;
}

/* 0 ok, 1 bad tag / not ours, 2 short or bad magic. */
static int unseal_blob(const unsigned char key[32], const unsigned char *blob,
                       size_t blen, unsigned char *out, size_t *outlen) {
    unsigned char enc[32], mac[32], want[TAG_LEN];
    if (blen < MAGIC_LEN + NONCE_LEN + TAG_LEN) return 2;
    if (memcmp(blob, MAGIC, MAGIC_LEN) != 0) return 2;
    size_t body = blen - TAG_LEN;
    derive_enc_mac(key, enc, mac);
    hmac_sha256(mac, 32, blob, body, want);
    if (memcmp(want, blob + body, TAG_LEN) != 0) return 1;
    *outlen = body - MAGIC_LEN - NONCE_LEN;
    keystream_xor(enc, blob + MAGIC_LEN, blob + MAGIC_LEN + NONCE_LEN, out, *outlen);
    return 0;
}

/* --------------------------------------------------------------------- main */

static void usage(void) {
    fprintf(stderr,
            "usage: snpreport --selftest\n"
            "       snpreport --key-hex|--key-id [--cmdline-file F] [--seed-file F]\n"
            "       snpreport --fp FILE\n"
            "       snpreport --seal|--unseal IN OUT [--cmdline-file F] [--seed-file F]\n");
}

int main(int argc, char **argv) {
    const char *cmdline_file = "/proc/cmdline", *seed_file = NULL;
    const char *pos[3] = {NULL, NULL, NULL};
    unsigned char seed[32], key[32];
    char cmdline[8192];
    int i, npos = 0;
    const char *verb;

    if (argc < 2) { usage(); return 2; }
    memcpy(seed, BENCH_VCEK, BENCH_VCEK_LEN);
    /* `--cmdline-file`/`--seed-file` may appear anywhere; the rest is verb,
     * then at most two paths. */
    for (i = 1; i < argc; i++) {
        if (strcmp(argv[i], "--cmdline-file") == 0 && i + 1 < argc) cmdline_file = argv[++i];
        else if (strcmp(argv[i], "--seed-file") == 0 && i + 1 < argc) seed_file = argv[++i];
        else if (npos < 3) pos[npos++] = argv[i];
        else { usage(); return 2; }
    }
    if (npos == 0) { usage(); return 2; }
    verb = pos[0];

    if (strcmp(verb, "--selftest") == 0) {
        unsigned char plain[64], blob[MAGIC_LEN + NONCE_LEN + MAX_PLAIN + TAG_LEN], back[MAX_PLAIN];
        size_t blen = 0, plen = 0;
        for (i = 0; i < 64; i++) plain[i] = (unsigned char)i;
        if (seal_blob(seed, plain, 64, blob, &blen) != 0) return 1;
        if (unseal_blob(seed, blob, blen, back, &plen) != 0 || plen != 64 ||
            memcmp(plain, back, 64) != 0) {
            fprintf(stderr, "snpreport selftest: round trip failed\n");
            return 1;
        }
        blob[blen - 1] ^= 1;
        if (unseal_blob(seed, blob, blen, back, &plen) != 1) {
            fprintf(stderr, "snpreport selftest: a tampered tag was accepted\n");
            return 1;
        }
        puts("selftest ok");
        return 0;
    }

    if (strcmp(verb, "--fp") == 0) {
        unsigned char buf[65536], digest[32];
        char b64[64], line[128];
        size_t len = 0;
        if (npos != 2) { usage(); return 2; }
        if (read_file(pos[1], buf, sizeof buf, &len) != 0) {
            perror(pos[1]);
            return 1;
        }
        sha256_ctx c;
        sha256_init(&c); sha256_update(&c, buf, len); sha256_final(&c, digest);
        base64_nopad(digest, 32, b64);
        snprintf(line, sizeof line, "SHA256:%s", b64);
        puts(line);
        return 0;
    }

    if (read_seed(seed_file, seed) != 0) {
        fprintf(stderr, "snpreport: the seed file is not 32 bytes\n");
        return 1;
    }
    if (read_cmdline(cmdline_file, cmdline, sizeof cmdline) != 0) {
        perror(cmdline_file);
        return 1;
    }
    derive_key(seed, cmdline, key);

    if (strcmp(verb, "--key-hex") == 0 || strcmp(verb, "--key-id") == 0) {
        char h[65];
        if (npos != 1) { usage(); return 2; }
        hex(key, 32, h);
        if (strcmp(verb, "--key-hex") == 0) printf("%s\n", h);
        else printf("key_sha256=%s\n", h);
        return 0;
    }

    if ((strcmp(verb, "--seal") == 0 || strcmp(verb, "--unseal") == 0) && npos == 3) {
        unsigned char in[MAGIC_LEN + NONCE_LEN + MAX_PLAIN + TAG_LEN];
        unsigned char out[MAGIC_LEN + NONCE_LEN + MAX_PLAIN + TAG_LEN];
        size_t inlen = 0, outlen = 0;
        if (read_file(pos[1], in, sizeof in, &inlen) != 0) {
            perror(pos[1]);
            return 1;
        }
        if (strcmp(verb, "--seal") == 0) {
            if (seal_blob(key, in, inlen, out, &outlen) != 0) {
                fprintf(stderr, "snpreport: seal failed (limit %d bytes)\n", MAX_PLAIN);
                return 1;
            }
        } else {
            int rc = unseal_blob(key, in, inlen, out, &outlen);
            if (rc != 0) {
                fprintf(stderr, "snpreport: not sealed to this guest, or damaged (rc %d)\n", rc);
                return 6;
            }
        }
        if (write_file(pos[2], out, outlen) != 0) {
            perror(pos[2]);
            return 1;
        }
        return 0;
    }

    usage();
    return 2;
}
