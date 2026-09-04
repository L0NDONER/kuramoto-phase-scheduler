/**
 * truth_manifest.h — compiled-in constitution for the 4-node swarm
 * (Mint, Pi1, Pi2, EC2).
 *
 * This is not a config file. It's a C header, compiled into truthd
 * (and anything else that needs to consult it) rather than read at
 * runtime from disk — a JSON/text file sitting next to the daemon
 * could be edited by anything running as the same user; a header only
 * changes by recompiling and redeploying the binary, which is a
 * deliberate, auditable act, not an ambient one. That's what "boot-time,
 * read-only" means in practice here.
 *
 * This does NOT add cryptographic identity pinning to the wire — the
 * existing intent channels (glyph/pi_intent_common.py, ec2_intent_common.py)
 * already do that with per-trust-domain HMAC keys ("a leaked EC2 key must
 * not grant Pi control and vice versa"). key_path below just says which
 * trust domain speaks for which node; it doesn't reimplement the check.
 *
 * The one property that actually matters and IS enforced here at boot
 * (see truth_manifest_selfcheck): TRUTH_ALLOWED must narrow monotonically
 * as tiers degrade. A lower tier can never permit a verb class the tier
 * above it forbids. Losing a node is only allowed to take authority away.
 */
#ifndef TRUTH_MANIFEST_H
#define TRUTH_MANIFEST_H

#include <string.h>

/* ---- identities ---------------------------------------------------- */

typedef enum {
    NODE_MINT = 0,
    NODE_PI1,
    NODE_PI2,
    NODE_EC2,
    NODE_COUNT
} NodeId;

typedef struct {
    NodeId id;
    const char *name;
    const char *host_ip;   /* quartz_node peer-health identity */
    const char *key_path;  /* intent-channel trust domain, relative to
                             * glyph/; NULL = no direct intent channel
                             * to this node (Mint is the operator, it
                             * doesn't receive signed intents itself) */
} NodeIdentity;

static const NodeIdentity TRUTH_NODES[NODE_COUNT] = {
    { NODE_MINT, "mint", "10.0.0.71",    NULL },
    { NODE_PI1,  "pi1",  "10.0.0.122",   "pi1_intent.key" },
    { NODE_PI2,  "pi2",  "10.0.0.174",   "pi2_intent.key" },
    { NODE_EC2,  "ec2",  "100.21.200.15","ec2_intent.key" },
};

/* ---- verb classes ---------------------------------------------------
 * Taken directly from the real INTENTS tables, not invented:
 *   glyph/pi_intent_listener.py:  uptime, whoami, reboot, restart_wg,
 *                                 restart_container
 *   glyph/ec2_intent_listener.py: start_wg_easy, restart_wg_easy,
 *                                 stop_wg_easy
 */

typedef enum {
    VERB_BENIGN_READ = 0,  /* uptime, whoami -- no state change, always fine */
    VERB_LOCAL_HEAL,        /* restart_wg, restart_container -- mutates only
                              the node running it */
    VERB_LOCAL_DESTRUCTIVE, /* reboot -- mutates only the node running it,
                              but takes it fully offline for a window */
    VERB_EC2_SELF,          /* start_wg_easy, restart_wg_easy, stop_wg_easy --
                              claims authority over the EC2 node specifically */
    VERB_CLASS_COUNT
} VerbClass;

/* Wire names for the CHECK protocol (truthd.c) and the gate hook
 * (glyph/truthd_client.py) -- one string table so both sides name verb
 * classes the same way instead of each hardcoding their own strings. */
static const char *VERB_NAMES[VERB_CLASS_COUNT] = {
    "BENIGN_READ", "LOCAL_HEAL", "LOCAL_DESTRUCTIVE", "EC2_SELF"
};

static inline int truth_verb_from_name(const char *name) {
    for (int v = 0; v < VERB_CLASS_COUNT; v++) {
        if (strcmp(name, VERB_NAMES[v]) == 0) return v;
    }
    return -1;
}

/* ---- quorum tiers ----------------------------------------------------
 * Derived from quartz_node's existing peer-health signal
 * (/tmp/quartz_peer_health.json: healthy flag per peer, silence +
 * phase-dev, see quartz_node.c). truthd computes which tier applies;
 * this header only says what each tier is allowed to authorize.
 */

typedef enum {
    TIER_FULL = 0,        /* EC2 + local triad (Mint, Pi1, Pi2) all healthy */
    TIER_LOCAL_TRIAD,      /* EC2 unreachable; Mint, Pi1, Pi2 agree */
    TIER_PARTITIONED,      /* local triad itself disagrees, or fewer than
                             2 of {Mint,Pi1,Pi2} are mutually healthy */
    TIER_COUNT
} QuorumTier;

static const char *TIER_NAMES[TIER_COUNT] = {
    "TIER_FULL", "TIER_LOCAL_TRIAD", "TIER_PARTITIONED"
};

/* allowed[tier][verb] -- 1 = permitted at this tier.
 * Row order must be monotonically non-increasing top to bottom for
 * every column; enforced by truth_manifest_selfcheck(), not just by
 * this comment. */
static const int TRUTH_ALLOWED[TIER_COUNT][VERB_CLASS_COUNT] = {
    /*                        BENIGN_READ  LOCAL_HEAL  LOCAL_DESTRUCTIVE  EC2_SELF */
    /* TIER_FULL         */ { 1,           1,          1,                 1 },
    /* TIER_LOCAL_TRIAD  */ { 1,           1,          0,                 0 },
    /* TIER_PARTITIONED  */ { 1,           0,          0,                 0 },
};

/* ---- boot-time self-check --------------------------------------------
 * Refuses to start if the table above ever grants a lower tier
 * something a higher tier forbids. This is the one piece of the
 * "constitution" that's mechanically checked rather than just
 * documented -- a bad edit to TRUTH_ALLOWED fails closed at startup
 * instead of silently widening authority under degradation.
 *
 * Returns NULL if the manifest is well-formed, else a static string
 * describing the first violation found (caller should refuse to start).
 */
static inline const char *truth_manifest_selfcheck(void) {
    for (int v = 0; v < VERB_CLASS_COUNT; v++) {
        for (int t = 1; t < TIER_COUNT; t++) {
            if (TRUTH_ALLOWED[t][v] > TRUTH_ALLOWED[t - 1][v]) {
                return "TRUTH_ALLOWED grants a lower tier more than a higher one";
            }
        }
    }
    return NULL;
}

#endif /* TRUTH_MANIFEST_H */
