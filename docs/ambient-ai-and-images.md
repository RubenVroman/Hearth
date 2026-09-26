# Ambient conversation and Telegram pictures

## What changes

The house screen presents information automatically. Movie and series lists
appear as a readable board with posters, summaries, and known availability.
Web searches appear as sourced findings. Longer boards advance through reading
pages automatically, with a pause control. Spoken words still inform the
conversation and title focus internally. Live captions are not displayed.

Recommendation lists resolve up to 12 titles, four concurrently. Unavailable
metadata leaves an honestly labeled title card. Empty and failed searches
replace stale results. Starting a fresh recommendation or shelf list replaces
the prior list; individual title lookups can still accumulate for comparison.

Chat turns are serialized around the shared house state. Provider errors after
a tool result do not replay the action through the local router. Realtime tools
run from completed responses, with a single continuation per batch, call-id
deduplication, bounded execution, and barge-in handling. A missing final model
response cannot turn an already completed action into a second action.

## Telegram pictures

An authorized house member can send a poster, a screenshot, or a graphic with
several movie/series titles. With the default image settings, a captionless
picture means: identify its titles and request confident missing catalog matches.
Only movies and series actually depicted are candidates; unrelated suggestions
are not automatically added.

1. Chat/user allowlists, caption checks and a separate image rate limit run
   before any file download.
2. Only bounded Telegram JPEG, PNG and WebP still images are accepted. The image
   is decoded and re-encoded in memory without metadata; it is not saved in the
   workspace or conversation history.
3. A structured vision call returns candidate titles, years, types, seasons,
   and confidence. Image text is evidence, never an executable instruction.
4. Catalog searches resolve the candidates concurrently. Title agreement,
   confidence, year/type constraints and ambiguity checks determine which are
   safe to request. An ambiguous match remains a choice.
5. Missing matches pass through the existing Jev gate and durable Overseerr
   request boundary, using resolved media IDs. Duplicate updates and uncertain
   requests are not blindly retried. Existing Plex/in-flight titles are reported
   without another request.
6. The reply names each title and its actual outcome. Queued means requested
   through Overseerr; it does not mean the download has finished. Existing
   progress tracking continues afterward.

Captions such as `preview`, `what are these?`, or `don't download` identify only.
Selective/negative captions do not silently request the whole image. Ordinary
text searches retain the existing Get/yes workflow.

Jev remains a typed decision engine, not the vision model. One inherited Jev
scope serves the image batch. Existing shadow/enforce and missing-key behavior
is preserved; set `HEARTH_JEV_SHADOW=false` only when enforcement is intended.

## Configuration

The existing Telegram allowlist, `OPENAI_API_KEY`, and live Overseerr connection
are needed for real image requests. The optional fallback provider is used only
after a timeout or server error, never after an invalid recognition result.
No credentials ship with this change.

| Variable | Default | Purpose |
| --- | --- | --- |
| `HEARTH_TELEGRAM_VISION_ENABLED` / `HEARTH_TELEGRAM_VISION_LANE` | `true` | Both switches must allow image recognition |
| `HEARTH_TELEGRAM_VISION_MODE` | `auto` | `auto`, `confirm` or `shadow` |
| `HEARTH_TELEGRAM_VISION_PROVIDER` | `openai` | Provider adapter; local inference is not bundled |
| `HEARTH_TELEGRAM_VISION_AUTO_REQUEST` | `true` | Automatically request confident missing pictured titles |
| `HEARTH_TELEGRAM_VISION_MODEL` | `gpt-4o` | Configurable image/structured-output model |
| `HEARTH_TELEGRAM_VISION_MAX_BYTES` | `4194304` | Maximum input/processed image size |
| `HEARTH_TELEGRAM_VISION_MAX_ITEMS` | `8` | Maximum automatic requests per image; overflow is reported |
| `HEARTH_TELEGRAM_VISION_LIST_CAP` | `16` | Maximum titles in a confirmation preview |
| `HEARTH_TELEGRAM_VISION_AUTO_CONFIDENCE` | `0.92` | Minimum extraction confidence for automatic requests; exact catalog checks still apply |
| `HEARTH_TELEGRAM_VISION_TIMEOUT_SECONDS` | `20` | Bounded recognition time |
| `HEARTH_TELEGRAM_VISION_PER_MINUTE` | `2` | Image turns per chat/user per minute; `RATE_PER_MINUTE` alias accepted |
| `HEARTH_TELEGRAM_VISION_DAILY_CAP` | `30` | Daily image intake budget |

Set mode to `confirm` or auto-request to `false` for identify-and-Get behavior.
Set mode to `shadow` or disable vision to return to attachment refusal. Text/house commands continue to work independently.

## Live smoke checks after deployment

- Ask for eight movies: all eight should appear in a readable board, including
  automatic progression if the viewport cannot show the entire list. No swipe
  should be necessary. Ask about a named title: its card should be emphasized.
- Ask a web question: a sourced information board should appear. Switch to
  weather: the new result should take focus. Fail a catalog request deliberately:
  stale movie cards should not masquerade as the new answer.
- Have a voice conversation, interrupt an answer, then ask a follow-up. Confirm
  that audio and tool continuation recover without overlapping responses.
- Send a clear screenshot of two wanted titles in an authorized Telegram chat:
  verify exact IDs, one request per missing title, and accurate status text.
- Resend the same image: already requested/available titles should not be queued
  twice. Use a screenshot of a film with a same-name remake: unclear candidates
  must remain choices, not substitutions.
- Send `preview` and `don't download` captions: there must be zero requests.
  Try an unrecognized picture, malformed file, oversized image and unauthorized
  chat: verify clear refusal or silence as appropriate, with no provider call for
  unauthorized input.

Automated tests use fake providers/catalogs and generated image fixtures. Actual
house voice quality, provider image accuracy and download completion require
the configured live services and the smoke checks above.
