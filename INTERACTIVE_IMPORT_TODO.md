# Interactive import review: proposed TODO

## Implementation status

The implementation is now present on `feature/interactive-import`. It includes the phased importer, Pixiv artwork grouping, provenance-aware tag review, per-image/shared modes, editable safety, configuration, CLI wiring, tests, and user documentation.

The complete workflow now uses one persistent full-screen terminal UI: URL entry, gallery-dl activity, metadata preparation, mode selection, tag review, and upload progress. It includes a dedicated filter, arrow-key navigation, Space toggles, inline tag/safety/duplicate controls, per-image queue and back navigation, and an **Import another** action. A formal provider-adapter interface remains future work; the internal grouping and provenance models are already provider-neutral.

## Context

The old `import.sh` helper from `tg-update` and `feature/booru-merge` repeatedly asks for a URL, fallback safety, and whether to add `potential_rels`. It then invokes `import-from-url` with `tagme` and any configured extra tags.

This is convenient for individual Pixiv imports, but `import-from-url` currently prepares and uploads every downloaded file in the same worker. A multipage Pixiv artwork therefore evaluates the same metadata separately for every page, and the user cannot inspect or correct the final tags before uploading.

## Goal

Add a terminal-assisted import workflow that:

- downloads media and metadata before uploading anything;
- groups all pages belonging to the same Pixiv artwork;
- evaluates source tags once per artwork;
- shows the final tags and safety before upload;
- lets the user remove any tag, add tags, and change safety;
- can review every downloaded image or apply one shared schema to a batch;
- clearly shows where each proposed tag came from;
- retains the existing non-interactive `import-from-url` workflow;
- can support providers other than Pixiv later without requiring a new review UI.

Image previews are explicitly out of scope.

## Proposed command

Use a dedicated command for the interactive workflow:

```console
szuru-toolkit interactive-import [OPTIONS] [URLS]...
```

When no URL is supplied, the TUI opens with its URL field focused. Supplying URLs opens the same TUI and starts downloading automatically. After upload, **Import another** cleans the completed session and returns to URL entry.

Proposed options:

```text
--review-mode [ask|each|shared]
--default-safety [safe|sketchy|unsafe]
--safety-policy [fallback|override]
--max-similarity FLOAT
--add-tags TAGS
--input-file FILE
--range RANGE
--cookies FILE
```

Existing download and upload settings such as conversion, shrinking, duplicate handling, and auto-tagging should continue to come from the normal toolkit configuration unless a real need for interactive overrides appears.

## Proposed configuration

Add a separate configuration section so interactive defaults do not change automated imports:

```toml
[interactive_import]
review_mode = "ask"
default_safety = "safe"
safety_policy = "fallback"
add_tags = ["tagme"]
max_similarity = 1.0
```

There are no locked or mandatory tags. Configured tags start selected but remain removable by the user.

Interactive imports default to `max_similarity = 1.0`, so perceptually similar images are uploaded and only exact matches are skipped. The user can choose **Change duplicate policy** during review to change the threshold for the current session.

Safety policies:

- `fallback`: use the source rating when available and `default_safety` otherwise. The result remains editable during review.
- `override`: initially replace every source rating with `default_safety`. The result remains editable during per-image review.

## Import phases

Refactor the URL importer into explicit phases:

1. **Download** media and metadata with gallery-dl.
2. **Prepare** normalized metadata without uploading.
3. **Group** files into artwork batches.
4. **Review** tags and safety, either per image or through a shared schema.
5. **Queue** per-image decisions while retaining Back navigation, or apply the shared schema.
6. **Upload** the reviewed batch while displaying structured progress and duplicate outcomes.
7. **Reconcile relations and clean up** temporary downloads.

Neither mode should create a permanent post before review is complete and upload begins.

## Artwork grouping

For Pixiv, group files by the Pixiv artwork ID from gallery-dl metadata. The generated source URL can be a fallback grouping key.

A multipage artwork should produce one metadata batch so tags are evaluated once. Per-image mode then expands that batch into separate review/upload steps:

```text
Pixiv artwork 12345678 - image 1 of 5
```

The first page starts with the evaluated tag set and safety. Its edits carry forward to the next page as editable defaults. Files should retain their current ordering.

The grouping implementation should use a provider-neutral key such as `(provider, work_id)`. A future provider adapter can then supply its own work ID without changing the review flow.

## Tag provenance

Do not represent prepared tags as plain strings until upload. Keep provenance alongside each tag, for example:

```python
TagCandidate(
    name="long_hair",
    origins={TagOrigin.SOURCE_MAPPED},
)
```

Proposed origins:

- `SOURCE`: read directly from provider metadata without changing the name.
- `SOURCE_MAPPED`: read from provider metadata and mapped to a canonical tag, such as a Pixiv tag resolved through Danbooru.
- `TOOL_DERIVED`: derived by the toolkit from other metadata, such as an artist tag derived from the Pixiv user.
- `TOOL_ADDED`: added by configuration, command-line options, or workflow choices, such as `tagme` or `potential_rels`.
- `USER_ADDED`: entered in the review UI.

If the same final name has several origins, merge the origins rather than displaying duplicate tags.

The UI should show short, visually distinct labels while leaving every tag selectable:

```text
[x] long_hair       [Pixiv -> canonical]
[x] original        [Pixiv]
[x] artist_name     [derived: artist]
[x] tagme           [tool: config]
[x] potential_rels  [tool: workflow]
[x] favorite        [user]
```

Color can supplement these labels, but meaning must not depend on color alone. Tool-added tags should be conspicuous, not protected: pressing Space must be able to deselect `tagme`, `potential_rels`, and configured `add_tags` like any other tag.

For the first version, provenance needs to be accurate for Pixiv only. Unknown providers can initially label metadata tags as `[source]` until a provider adapter supplies a better name.

## Per-image review mode

`review_mode = "each"` stops once per downloaded image. Pixiv tags are still evaluated once per artwork; edits made for one page carry forward as the starting state for the next page and remain editable.

Example screen:

```text
Pixiv artwork 12345678 - image 1 of 5

Tags (Space toggles, Enter accepts)
[x] artist_name     [derived: artist]
[x] character_name  [Pixiv -> canonical]
[x] long_hair       [Pixiv -> canonical]
[x] tagme           [tool: config]
[ ] potential_rels  [tool: workflow]

Safety
( ) safe
(x) sketchy         [Pixiv]
( ) unsafe

Actions
> Accept and upload image
  Add tag
  Skip image
  Abort import
```

All proposed tags start selected. The primary editing action is deselecting unwanted tags. Search/filter support is desirable when an image has many tags.

Changing safety in the UI should mark it as user-selected in the review summary.

## Shared-schema review mode

`review_mode = "shared"` should review the batch once without incorrectly copying artwork-specific tags to unrelated artworks.

Build a union of proposed tags across all artwork batches and show how often each tag occurs:

```text
Detected/derived tags
[x] long_hair       [Pixiv -> canonical, 7/10 artworks]
[x] artist_name     [derived: artist, 3/10 artworks]
[ ] text            [Pixiv -> canonical, 2/10 artworks]

Tags added to every artwork
[x] tagme           [tool: config]
[x] potential_rels  [tool: workflow]
```

Schema behavior:

- A selected detected or derived tag is retained only on artworks where it was originally proposed.
- A deselected detected or derived tag is removed everywhere it occurs in the batch.
- A selected common/tool/user tag is added to every artwork.
- Any tag, including a configured tag, can be deselected.
- The schema chooses one safety policy for the batch: retain per-artwork source safety with fallback, or force one safety level for all artworks.

This is an overlay schema rather than exact replacement. It preserves artwork-specific metadata while allowing one review stop for a large import.

Before upload, show a compact summary with artwork/page counts, tags removed globally, common tags added, and the safety policy.

## Internal model

Introduce small data objects instead of passing partially mutated metadata dictionaries through the entire workflow:

```text
DownloadedItem
  file path
  raw metadata

PreparedItem
  file path
  provider
  work ID
  source
  proposed tags with provenance
  proposed safety with provenance

ArtworkBatch
  provider and work ID
  ordered files
  shared proposed metadata
  review decision

ImportSchema
  retained detected tags
  removed detected tags
  common added tags
  safety policy and safety value
```

The uploader should receive ordinary final tag strings and safety only after review is complete. Interactive UI concerns must not leak into `upload_media`.

## Refactoring TODO

- [x] Extract gallery-dl invocation and downloaded-file discovery from `import_from_url.main()`.
- [x] Extract metadata preparation into a function that has no upload side effects.
- [x] Avoid calling Pixiv/Danbooru tag conversion once per page of the same artwork.
- [x] Add provider-neutral `DownloadedItem` and `ArtworkBatch` models.
- [x] Implement Pixiv artwork ID extraction and grouping.
- [x] Preserve tag provenance throughout metadata preparation.
- [x] Merge duplicate final tag names while retaining all origins.
- [x] Keep the existing non-interactive importer using the refactored preparation/upload functions.
- [x] Add `INTERACTIVE_IMPORT_DEFAULTS` and `[interactive_import]` configuration loading.
- [x] Add the `interactive-import` Click command and options.
- [x] Move URL entry, download activity, metadata preparation, and retry into the persistent TUI.
- [x] Capture gallery-dl output without allowing it to paint over the Textual screen.
- [x] Add **Import another** and keep temporary-download cleanup owned by the TUI session.
- [x] Implement the review-mode selection screen.
- [x] Skip the review-mode picker for a single image and open shared review directly.
- [x] Implement searchable per-image tag selection and safety editing.
- [x] Replace questionnaire prompts with one persistent full-screen review UI.
- [x] Implement shared-schema union, occurrence counts, and overlay behavior.
- [x] Implement inline add-tag, per-image queue/skip/back/apply-to-artwork, and abort actions.
- [x] Add upload progress and structured logs for uploaded, exact-match, similarity-skip, and failed images.
- [x] Ensure Ctrl-C and Ctrl-D abort cleanly before upload.
- [x] Upload only reviewed and accepted images or shared artwork batches.
- [x] Preserve relation reconciliation, duplicate handling, ordering, and temporary-directory cleanup.
- [x] Document the command and configuration in `README.md` and `config_sample.toml`.

## Test TODO

- [x] A multipage Pixiv artwork is grouped for one metadata evaluation and expanded into per-image review items.
- [x] Pixiv tag conversion runs once per artwork rather than once per page.
- [ ] Accepted tags and safety are applied to every page of an artwork.
- [ ] Every tag origin is classified and displayed correctly.
- [ ] Tags with multiple origins are merged without losing provenance.
- [x] Configured and tool-added tags start selected but can be removed.
- [x] User-added tags receive `USER_ADDED` provenance.
- [x] Per-image mode stops once per downloaded image and carries reviewed state between pages of one artwork.
- [x] Shared mode retains selected detected tags only where they originally occurred.
- [x] Shared mode removes deselected tags from every matching artwork.
- [x] Shared common tags are added to every accepted artwork.
- [ ] Fallback safety respects source safety when present.
- [x] Override safety replaces source safety.
- [x] Safety remains editable in per-image mode.
- [ ] Skipped artworks are not uploaded.
- [x] Aborting review uploads nothing.
- [ ] Final confirmation rejection uploads nothing.
- [x] The existing non-interactive `import-from-url` tests and behavior remain valid.

## Initial scope and future providers

The first implementation should target Pixiv metadata and Pixiv multipage grouping. Provider-specific behavior should live behind a small adapter interface responsible for:

- identifying the provider;
- finding a stable work ID;
- naming source-origin labels for the UI;
- extracting raw tags, artist information, source, and safety.

The generic preparation, schema, review, and upload stages should not contain Pixiv-only conditionals beyond selecting the adapter. Additional sources can then be added incrementally.

## Completion criteria

The first version is complete when a user can submit a Pixiv artwork or batch URL, review and immediately upload every image or apply one shared overlay schema, distinguish source-derived tags from tool-added tags, remove any proposed tag, edit safety, and upload all accepted pages without repeated per-page tag evaluation.
