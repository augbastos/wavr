/* An outside consumer of the C ABI: plain C, the public header only, linked
 * against the shared library -- the way JNI, Swift or P/Invoke will see it.
 * If this stops compiling or passing, the ABI broke for everyone. */
#include <stdio.h>
#include <string.h>

#include "wavr/wavr.h"

static int failed = 0;
#define CHECK(cond)                                           \
  do {                                                        \
    if (!(cond)) {                                            \
      fprintf(stderr, "FAIL line %d: %s\n", __LINE__, #cond); \
      failed = 1;                                             \
    }                                                         \
  } while (0)

int main(void) {
  char buf[256];
  unsigned char frame[30];
  int n;

  /* Versioning: this consumer was built against the header it includes. */
  CHECK(wavr_abi_compatible(WAVR_ABI_VERSION_MAJOR, WAVR_ABI_VERSION_MINOR) == 1);
  CHECK(wavr_abi_compatible(WAVR_ABI_VERSION_MAJOR + 1, 0) == 0);
  CHECK(wavr_abi_compatible(WAVR_ABI_VERSION_MAJOR, WAVR_ABI_VERSION_MINOR + 1) == 0);
  CHECK(wavr_abi_version() == WAVR_ABI_VERSION);
  CHECK(wavr_version() != NULL && strlen(wavr_version()) > 0);
  CHECK(strcmp(wavr_error_string(WAVR_ERR_ARGUMENT), wavr_error_string(12345)) != 0);

  /* NULL arguments are errors, never crashes. */
  CHECK(wavr_status_exit_code(NULL, NULL) == WAVR_ERR_PARSE);
  CHECK(wavr_compute_tier(4096, 4, NULL, buf, sizeof buf) == WAVR_ERR_ARGUMENT);
  CHECK(wavr_framer_feed(NULL, NULL, 0) == WAVR_ERR_ARGUMENT);
  CHECK(wavr_snapshot_json(NULL, buf, sizeof buf) == WAVR_ERR_ARGUMENT);
  CHECK(wavr_snapshot_exit_code(NULL) == WAVR_ERR_ARGUMENT);
  CHECK(wavr_snapshot_fetch(NULL, NULL, NULL, 0) == NULL);
  wavr_snapshot_free(NULL);
  wavr_framer_free(NULL);

  /* Caller-owned buffers: the size query, and truncation that stays terminated. */
  n = wavr_compute_tier(8192, 8, "linux", NULL, 0);
  CHECK(n == 4);                                   /* "high" */
  memset(buf, 'x', sizeof buf);
  CHECK(wavr_compute_tier(8192, 8, "linux", buf, 3) == 4);
  CHECK(buf[2] == '\0' && strcmp(buf, "hi") == 0);

  /* A stateful handle: create, use, free. */
  {
    wavr_framer* f = wavr_framer_new();
    CHECK(f != NULL);
    CHECK(wavr_framer_next(f, frame) == 0);
    CHECK(wavr_framer_feed(f, (const uint8_t*)"\x01\x02", 2) == WAVR_OK);
    CHECK(wavr_framer_next(f, frame) == 0);
    wavr_framer_free(f);
  }

  /* The client snapshot, from answers: a Core that did not answer is a
   * snapshot saying so, with exit code 2 -- not a NULL. */
  {
    wavr_snapshot* s = wavr_snapshot_from_answers(NULL, NULL, NULL, 0, "no route");
    CHECK(s != NULL);
    CHECK(wavr_snapshot_exit_code(s) == 2);
    n = wavr_snapshot_json(s, NULL, 0);
    CHECK(n > 0 && n < (int)sizeof buf);
    CHECK(wavr_snapshot_json(s, buf, sizeof buf) == n);
    CHECK(strstr(buf, "\"reachable\":false") != NULL);
    wavr_snapshot_free(s);

    s = wavr_snapshot_from_answers("{\"state\":\"healthy\"}",
                                   "{\"total\":0,\"could_not_check\":[]}", "{not json", 1, NULL);
    CHECK(s != NULL && wavr_snapshot_exit_code(s) == 0);
    wavr_snapshot_free(s);
  }

  /* An unpinned fetch off loopback is refused before any socket opens. */
  {
    wavr_snapshot* s = wavr_snapshot_fetch("https://192.0.2.1:8000", "t", NULL, 500);
    CHECK(s != NULL && wavr_snapshot_exit_code(s) == 2);
    n = wavr_snapshot_json(s, buf, sizeof buf);
    CHECK(n > 0 && strstr(buf, "only a loopback Core may skip") != NULL);
    wavr_snapshot_free(s);
  }

  printf(failed ? "c_consumer: FAILED\n" : "c_consumer: passed\n");
  return failed;
}
