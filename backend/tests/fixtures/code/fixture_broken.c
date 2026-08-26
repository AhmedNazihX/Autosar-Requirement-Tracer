/* Hand-written fixture: a file tree-sitter cannot fully parse.
 *
 * Written from scratch for this test suite. The real sources do this to
 * themselves with #if branches that open braces one arm never closes, so the
 * indexer must recover the units around the damage instead of aborting.
 */

/* @req 4.0.3/CANIF700 */
void Fixture_Before(void)
{
    return;
}

#if defined(FIXTURE_BROKEN)
void Fixture_Damaged(void)
{
    if (x { /* deliberately unbalanced */
#endif

/* @req 4.0.3/CANIF701 */
void Fixture_After(void)
{
    return;
}
