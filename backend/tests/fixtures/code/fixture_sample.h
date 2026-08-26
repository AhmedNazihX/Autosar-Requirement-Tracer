/* Hand-written fixture: header declarations that carry annotations.
 *
 * Written from scratch for this test suite. Prototypes in the real headers
 * do carry @req/!req annotations, so they must produce CodeUnits too.
 */
#ifndef FIXTURE_SAMPLE_H
#define FIXTURE_SAMPLE_H

/* @req 4.0.3/CANIF725 */
int Fixture_Transmit(const struct Fixture_PduType *pdu);

/* !req 4.0.3/CANIF317 */
void Fixture_MainFunction(void);

typedef void (*Fixture_CallbackType)(unsigned char hoh);

#endif /* FIXTURE_SAMPLE_H */
