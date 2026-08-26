/* Hand-written fixture for ReqTrace's C chunker and annotation scanner.
 *
 * Written from scratch for this test suite — no line is copied from any
 * real project. It reproduces every declaration and annotation shape the
 * openAUTOSAR sources use (and nothing else), so the expectations in
 * tests/test_code_indexer.py and tests/test_annotations.py can pin exact
 * 1-based line spans.
 *
 * @req 4.0.3/CANIF672
 * !req CANIF058
 */

#include "fixture_sample.h"

/* @req 4.0.3/CANIF010 */
#define FIXTURE_MAX_CHANNELS 4u

#define FIXTURE_IS_VALID(id) ((id) < FIXTURE_MAX_CHANNELS)

/* @req 4.0.3/CANIF020 */
typedef struct {
    unsigned char hoh;
    unsigned int canId;
} Fixture_PduType;

typedef enum {
    FIXTURE_UNINIT = 0,
    FIXTURE_READY
} Fixture_StateType;

struct Fixture_Global {
    Fixture_StateType state;
};

typedef struct Fixture_Global Fixture_GlobalType;

static Fixture_GlobalType FixtureGlobal;

/**
 * Transmit one PDU.
 *
 * @req 4.0.3/CANIF661
 * @req 4.3.0/CAN416
 */
int Fixture_Transmit(const Fixture_PduType *pdu)
{
    if (pdu == 0) {
        /* !req 4.0.3/CANIF316 */
        return -1;
    }
    /* @req 9.9.9/ZZZ001 */
    return 0;
}

/* @req CANTP133 */
void Fixture_MainFunction(void)
{
    FixtureGlobal.state = FIXTURE_READY;
}

/* !req CANIF142 */


static void Fixture_Unreferenced(void)
{
}

static void Fixture_Empty(void)
{
}
