# Process log

How this tool got built: the problems that came up while printing real books, what went wrong, and the decisions that shaped the design. Built with AI-assisted development. The binding requirements, design decisions, testing on physical prints, and validation are mine.

---

### 1. Starting point: existing tools couldn't do the job
**Goal:** Impose an 88-page facsimile into a 30 × 40 mm handbound book, printed on Super B (13 × 19 in) sheets.
**Problem:** Affinity Publisher's booklet mode does one saddle-stitched signature for the whole document. A sewn book needs many small signatures, and pages this tiny need to be ganged dozens to a sheet. The free imposition tools I found didn't handle that combination; the ones that did were expensive prepress software.
**Decision:** Build a dedicated tool, with every binding parameter in one config block, so it could be reused for every book on the shelf, not just this one.

### 2. Choosing signature size for the binding, not the math
**Goal:** Pick signature size. The first suggestion was 3-folio (12-page) signatures, which don't divide 88 pages evenly.
**What I caught:** At 30 mm, the real constraint is my cutter. A large guillotine is bad at trimming a tiny bound text block, so the priority was minimizing fore-edge creep and post-binding trim.
**Decision:** 2-folio (8-page) signatures. They fold crisply at this scale, creep is negligible, and 88 pages divides into exactly 11.

### 3. Page order from a formula, not a lookup table
**Goal:** Support any signature size without hand-writing layouts.
**Decision:** Derive the nested-saddle order from a general formula, so changing `folios_per_sig` from 2 to 3 to 4 just works. Before building, I locked the requirements: crop off by default, the same crop spec on every page, and a synthetic demo mode to check geometry before using real art.

### 4. Uneven page counts
**Problem:** Most books don't divide evenly into signatures, and a short "runt" signature sews poorly at miniature scale.
**Decision:** Keep all signatures equal and pad with blank leaves (at the back by default) instead of making an odd one.

### 5. Bleed and cut lines for trimming after binding
**Problem:** Images that stop exactly at the trim line leave white slivers when the cut drifts.
**Decision:** Render each page at trim size plus bleed, draw a faint perimeter cut line at the true trim edge, and let everything outside it fall away on the cutter. Perimeter lines instead of corner crosses, because crosses are hard to follow at this size.

### 6. Fine line art disappearing at 30 mm (Bridgman, *Constructive Anatomy*)
**Problem:** Anatomical line drawings reduced to 30 × 40 mm lost their thinnest lines.
**Decision:** A grayscale mode plus a line-art boost: a levels adjustment and a blended one-pixel dilation that thickens fine strokes before downsizing.

### 7. Gutters landing on the wrong edge (Alfonso X manuscript, HathiTrust)
**Problem:** The scans had their wide margins on the opposite side from normal recto/verso layout, so imposed pages put the gutter at the fore-edge instead of the spine.
**Decision:** Add `--mirror`, which swaps which half of the folio each page occupies so the binding margin lands at the fold.

### 8. The wrong image grabbed from a web-saved PDF (Book of Kells, Archive.org)
**Problem:** The loader picked the largest embedded image on each page. This PDF was saved from a browser, with many images per page, so "largest" was often the wrong one.
**Decision:** Add `--render`, which rasterizes each whole page as it appears instead of extracting images.

### 9. Running out of memory on large books
**Problem:** The 286-page Kells facsimile, in color at print resolution, was too large to hold in memory.
**Decision:** Write each prepared page to a disk cache and keep only file paths in memory. A side benefit: an interrupted run resumes where it stopped.

### 10. Real printers don't register front to back
**Problem:** On hand-fed duplex printing, the back side shifted slightly relative to the front. At 30 mm, half a millimeter is visibly crooked.
**Decision:** A measured offset setting. Print, fold a folio, measure the drift through the paper, and the tool shifts the back sheet to compensate.

### 11. Facsimile clean-up: black mattes and yellowed paper
**Problems:** Digitized manuscripts often sit on a black border, and aged paper prints muddy and dark at small sizes.
**Decisions:**
- A `page` crop mode that finds the paper edge in each scan using one bounding box for all four sides, so a stray light pixel can't shift just one edge.
- Paper-color correction using per-channel multiplication (a white-balance adjustment) instead of adding brightness. Multiplying shifts the paper toward white or cream while ink stays dark; adding would turn black ink gray.

### 12. Batch processing with per-book settings
**Problem:** Each book needed different options, and retyping command-line flags was error-prone.
**Decision:** Encode settings in the filename (`kells__render__page-30x40.pdf`) and process a whole folder in one run.

### 13. Preparing for public release
- Replaced the PDF library with a permissively licensed one, so the tool can be shared and built on freely.
- Reset defaults from my own printer and book settings to neutral ones, and changed the default layout to a 3.5 × 5 in book on US Letter.
- Fixed a cache bug: changing a prep setting reused pages prepared under the old settings. The cache is now keyed on those settings.
- Added a clear error when pages are too large for the sheet, instead of a crash.
