#!/usr/bin/env python3
"""
PARAMETRIC SADDLE-NESTED IMPOSITION TOOL
=========================================
Fixed page size + binding geometry, VARIABLE page count. Feed it a source PDF
(or run the synthetic demo) and it emits a print-ready imposed PDF: front sheet(s)
+ duplex-mirrored back sheet(s), with perimeter cut lines and fold ticks.

Defaults: a 3.5x5in (89x127mm) pocket book on US Letter sheets, 16-page
signatures. Proven down to a 30x40mm miniature; all geometry is configurable.

Requires: pillow, numpy, reportlab, pypdfium2  (all permissively licensed)

----------------------------------------------------------------------------
PIPELINE (per source page -> final sheet):
  1. per-side CROP of the source image  (remove photographic border)   [off by default]
  1b. PAPER COLOR NEUTRALIZE (for yellowed/aged manuscript pages)      [off by default]
  2. FIT cropped image into the trim cell (fill / fit / autotrim)
  3. BLEED: extend image outside the trim line so guillotine leaves no sliver
  4. draw PERIMETER cut line at the true trim boundary
  5. IMPOSE folios onto the sheet (front upright; back mirrored for hand flip)
Page counts that don't fill the last signature are AUTO-PADDED with blank leaves.
----------------------------------------------------------------------------
"""

import io, sys, math, argparse
from PIL import Image, ImageFilter, ImageDraw, ImageFont
import numpy as np
from reportlab.pdfgen import canvas
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader

# ===========================================================================
# CONFIG  -- everything you'd change per project lives here
# ===========================================================================
CFG = dict(
    # ---- page (trim) size: the finished leaf ----
    page_w_mm        = 89.0,       # 3.5 x 5 in pocket book
    page_h_mm        = 127.0,

    # ---- binding scheme ----
    folios_per_sig   = 4,          # 4 folios = 16pp signatures (nested saddle)
    fold             = "vertical", # "vertical" = side-by-side pages, opens L<->R
                                   # "horizontal" = stacked pages, opens like a calendar

    # ---- sheet ----
    sheet_w_mm       = 215.9,      # US Letter (A4 = 210 x 297, Super B = 330 x 483)
    sheet_h_mm       = 279.4,
    safe_margin_mm   = 8.0,        # keep all content inside this (clears non-borderless)
    gutter_mm        = 5.0,        # space between folio units (cut alleys)

    # ---- bleed & marks ----
    bleed_mm         = 1,          # image extends this far OUTSIDE the trim line
    mark_style       = "perimeter",# "perimeter" | "perimeter_gap" | "crosses"
    fold_ticks       = True,       # short ticks at the fold center, top & bottom
    mark_gray        = 0.88,       # 0=black .. 1=white; faintness of cut lines

    # ---- duplex ----
    flip_edge        = "long",     # "long" = flip L<->R (back mirrored horizontally)
                                   # "short" = flip top<->bottom (back mirrored vertically)
    duplex_offset_x_mm = 0.0,      # correct through-paper drift caused by printer's
                                   # per-pass shift. Fold a printed folio in half and
                                   # measure the offset between front and back. Set this
                                   # to that measured value (positive = back sits to the
                                   # RIGHT of the front through the paper -- most common
                                   # case with left-side tray guides / left dead zones).
    duplex_offset_y_mm = 0.0,      # same for vertical drift (rare on long-edge flip; can
                                   # matter with short-edge flip or paper skew).

    # ---- image handling ----
    color_mode       = "rgb",      # "rgb" for color art; "gray" for line art
    render_dpi       = 600,        # rasterization dpi at final size
    fit_mode         = "fit",      # "fill" (cover, may crop) | "fit" (letterbox) | "autotrim"
                                   # autotrim = trim source whitespace, then fit
    line_art_boost   = False,      # True applies the C+ levels+dilation (line art only)

    # ---- per-side source crop (off by default) ----
    # each side: "none" | float mm (fixed)
    #          | "auto" (trim BACKGROUND to content -- for white/blank margins)
    #          | "page" (find the PAPER edge -- for stripping a black digitization
    #                   matte, so the manuscript page ends up centered in the trim)
    crop_top         = "none",
    crop_right       = "none",
    crop_bottom      = "none",
    crop_left        = "none",
    crop_auto_thresh = 240,        # for "auto": pixels brighter than this = background
    crop_auto_pad_mm = 0.0,        # padding to leave around detected content
    crop_page_thresh = 40,         # for "page": pixels brighter than this = paper
                                   # (raise if your matte isn't very black; lower
                                   # if paper is very dark/yellowed)

    # ---- paper color correction (for yellowed / aged pages) ----
    paper_color      = "none",     # "none" | "auto" | "#EED9A8" (hex) | (r,g,b) tuple
    paper_target     = "white",    # what the paper color should become:
                                   # "white" (bleach to 255,255,255) | hex like "F2E8DB"
                                   # | (r,g,b) — use a warm cream to preserve aged look
    paper_strength   = 1.0,        # 0.0 = no shift, 1.0 = paper hits target exactly
                                   # 0.6-0.8 keeps some of the original character
    paper_sample_pct = 95,         # for "auto": percentile of brightness treated as paper

    # ---- pad policy ----
    pad_position     = "back",     # where blank leaves go: "back" | "front"
    mirror_pages     = False,      # swap page-within-folio placement (for sources whose
                                   # binding margin is on the reversed side); keeps gutters
                                   # at the fold/spine instead of the fore-edge
)

# ===========================================================================
# DERIVED GEOMETRY
# ===========================================================================
def geom(cfg):
    g = {}
    g['PW'] = cfg['page_w_mm'] * mm
    g['PH'] = cfg['page_h_mm'] * mm
    # a flat folio holds 2 pages; orientation depends on fold
    if cfg['fold'] == "vertical":
        g['FW'] = 2 * g['PW']; g['FH'] = g['PH']      # pages side by side
    else:
        g['FW'] = g['PW'];     g['FH'] = 2 * g['PH']  # pages stacked
    g['SW'] = cfg['sheet_w_mm'] * mm
    g['SH'] = cfg['sheet_h_mm'] * mm
    g['MAR'] = cfg['safe_margin_mm'] * mm
    g['GUT'] = cfg['gutter_mm'] * mm
    g['BLEED'] = cfg['bleed_mm'] * mm
    # how many folio units fit per sheet side, given margins+gutters
    avail_w = g['SW'] - 2*g['MAR']
    avail_h = g['SH'] - 2*g['MAR']
    g['COLS'] = int((avail_w + g['GUT']) // (g['FW'] + g['GUT']))
    g['ROWS'] = int((avail_h + g['GUT']) // (g['FH'] + g['GUT']))
    if g['COLS'] < 1 or g['ROWS'] < 1:
        fw, fh = g['FW']/mm, g['FH']/mm
        raise ValueError(
            f"A flat folio ({fw:.1f} x {fh:.1f} mm) does not fit on a "
            f"{cfg['sheet_w_mm']} x {cfg['sheet_h_mm']} mm sheet with "
            f"{cfg['safe_margin_mm']} mm margins. Use a smaller page size, a larger "
            f"sheet, or try the other fold direction.")
    g['PER_SHEET'] = g['COLS'] * g['ROWS']            # folios per sheet side
    # center the grid
    grid_w = g['COLS']*g['FW'] + (g['COLS']-1)*g['GUT']
    grid_h = g['ROWS']*g['FH'] + (g['ROWS']-1)*g['GUT']
    g['OX'] = g['MAR'] + (avail_w - grid_w)/2
    g['OY'] = g['MAR'] + (avail_h - grid_h)/2
    g['avail_w'], g['avail_h'] = avail_w, avail_h
    return g

# ===========================================================================
# SIGNATURE / FOLIO PLANNING  (the page-order math, generalized)
# ===========================================================================
def plan_signatures(n_pages, folios_per_sig, pad_position):
    """
    Returns (total_after_pad, list_of_folios). Each folio is a dict with the four
    page numbers it carries and which signature/role it is. Page numbers are
    1-based into the PADDED book; 0 means BLANK leaf.

    Nested saddle imposition for a signature of F folios (=> 4F pages, base b):
      folio i (i=0 outermost .. F-1 innermost) carries, with pages numbered
      1..4F within the signature:
        front-left  = 4F - 2i
        front-right = 1  + 2i
        back-left   = 2  + 2i
        back-right  = 4F - 1 - 2i
      then add the signature base b to each.
    """
    pp_per_sig = 4 * folios_per_sig
    n_sigs = math.ceil(n_pages / pp_per_sig)
    total = n_sigs * pp_per_sig
    pad = total - n_pages

    # map "logical page slot 1..total" -> source page number or 0(blank)
    if pad_position == "front":
        # blanks first
        slot_to_src = [0]*pad + list(range(1, n_pages+1))
    else:
        slot_to_src = list(range(1, n_pages+1)) + [0]*pad

    folios = []
    for s in range(n_sigs):
        b = s * pp_per_sig
        F = folios_per_sig
        for i in range(F):
            fl = b + (4*F - 2*i)
            fr = b + (1 + 2*i)
            bl = b + (2 + 2*i)
            br = b + (4*F - 1 - 2*i)
            folios.append(dict(
                sig=s+1, role=f"f{i+1}",
                fL=slot_to_src[fl-1], fR=slot_to_src[fr-1],
                bL=slot_to_src[bl-1], bR=slot_to_src[br-1],
            ))
    return total, folios, n_sigs, pad

# ===========================================================================
# PAPER COLOR NEUTRALIZATION  (for yellowed / aged manuscript pages)
# ===========================================================================
def parse_paper_spec(spec):
    """Turn config value into a tuple, the string 'auto', or None.
       Accepts '#EED9A8', 'EED9A8', 'auto', 'none', or an (r,g,b) tuple already."""
    if spec is None or spec == "none":
        return None
    if isinstance(spec, (tuple, list)):
        return tuple(int(v) for v in spec)
    s = str(spec).strip()
    if s.lower() == "auto":
        return "auto"
    if s.lower() == "none" or s == "":
        return None
    s = s.lstrip("#")
    try:
        return (int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16))
    except (ValueError, IndexError):
        print(f"  ! bad paper color '{spec}' (expected 'auto', 'none', or hex like EED9A8)")
        return None

def sample_paper_rgb(img, percentile=95):
    """Estimate paper color from the image itself.
       Take pixels brighter than the Nth percentile (that's the paper, not the ink)
       and average their RGB. Returns (r,g,b) or None if the page is all dark."""
    rgb = np.asarray(img.convert("RGB"))
    L   = rgb.mean(axis=2)
    thr = np.percentile(L, percentile)
    mask = L >= thr
    if not mask.any():
        return None
    return tuple(int(v) for v in rgb[mask].mean(axis=0))

def neutralize_paper(img, paper_rgb, strength=1.0, target_rgb=(255,255,255)):
    """Scale each channel so paper_rgb maps toward target_rgb.
       target_rgb=(255,255,255)  -> bleach to pure white
       target_rgb=(242,232,219)  -> preserve warm cream character
       Per-channel multiplier: 1 + strength * (T_c/P_c - 1).
       strength=0 -> no-op; strength=1 -> paper hits target exactly.
       Ink stays dark because near-zero values don't move much when multiplied.
       This is a diagonal-matrix white balance (same math a camera uses for
       a custom white point). Multiplicative rather than additive keeps blacks
       black; an additive shift would gray out the ink."""
    if paper_rgb is None or strength <= 0:
        return img
    was_gray = img.mode != "RGB"
    if was_gray:
        img = img.convert("RGB")
    arr = np.asarray(img).astype(np.float32)
    pr, pg, pb = [max(int(v), 1) for v in paper_rgb]  # avoid /0
    tr, tg, tb = [int(v) for v in target_rgb]
    mult = np.array([
        1 + strength * (tr/pr - 1),
        1 + strength * (tg/pg - 1),
        1 + strength * (tb/pb - 1),
    ], dtype=np.float32)
    arr *= mult
    np.clip(arr, 0, 255, out=arr)
    out = Image.fromarray(arr.astype("uint8"), "RGB")
    return out.convert("L") if was_gray else out

# ===========================================================================
# IMAGE PROCESSING
# ===========================================================================
def per_side_crop(img, cfg):
    """Crop each side independently.
       Per-side values:
         "none"        -- leave this side alone
         float mm      -- shave this many mm off this side
         "auto"        -- trim outer BACKGROUND up to first content pixel
                          (mask: pixel < crop_auto_thresh; catches ink on paper)
         "page"        -- trim outer MATTE up to first paper pixel
                          (mask: pixel > crop_page_thresh; catches paper on black
                          -- strips a black digitization border so the manuscript
                          page ends up centered in the trim cell)
       When all four sides are "page", the crop stays consistent across pages
       (each page's bounding box is computed once, not four independent edges),
       so pages align through the paper on duplex."""
    w, h = img.size
    px_per_mm = cfg['render_dpi'] / 25.4
    # start with full box
    L, T, R, B = 0, 0, w, h
    arr = None
    def ensure_arr():
        nonlocal arr
        if arr is None:
            arr = np.asarray(img.convert("L"))
        return arr
    thr = cfg['crop_auto_thresh']
    pad = int(cfg['crop_auto_pad_mm'] * px_per_mm)
    page_thr = cfg.get('crop_page_thresh', 40)

    def auto_edge(side):
        a = ensure_arr()
        mask = a < thr  # True where content (dark on light bg)
        if not mask.any():
            return None
        cols = mask.any(axis=0); rows = mask.any(axis=1)
        x0 = np.argmax(cols); x1 = w - np.argmax(cols[::-1])
        y0 = np.argmax(rows); y1 = h - np.argmax(rows[::-1])
        return dict(left=x0, right=x1, top=y0, bottom=y1)[side]

    # cache the paper bbox so all four sides come from ONE detection pass --
    # otherwise a stray light pixel on one side could shift only one edge
    _paper_bbox = [None]
    def page_edge(side):
        if _paper_bbox[0] is None:
            a = ensure_arr()
            mask = a > page_thr  # True where paper (light on dark matte)
            if not mask.any(): return None
            cols = mask.any(axis=0); rows = mask.any(axis=1)
            x0 = int(np.argmax(cols)); x1 = w - int(np.argmax(cols[::-1]))
            y0 = int(np.argmax(rows)); y1 = h - int(np.argmax(rows[::-1]))
            _paper_bbox[0] = (x0, y0, x1, y1)
        pb = _paper_bbox[0]
        return dict(left=pb[0], top=pb[1], right=pb[2], bottom=pb[3])[side]

    for side, var in [("left",cfg['crop_left']),("right",cfg['crop_right']),
                      ("top",cfg['crop_top']),("bottom",cfg['crop_bottom'])]:
        if var == "none":
            continue
        if var == "auto":
            e = auto_edge(side)
            if e is None: continue
            if side=="left":   L = max(L, e - pad)
            if side=="right":  R = min(R, e + pad)
            if side=="top":    T = max(T, e - pad)
            if side=="bottom": B = min(B, e + pad)
        elif var == "page":
            e = page_edge(side)
            if e is None: continue
            if side=="left":   L = max(L, e - pad)
            if side=="right":  R = min(R, e + pad)
            if side=="top":    T = max(T, e - pad)
            if side=="bottom": B = min(B, e + pad)
        else:  # fixed mm
            px = int(float(var) * px_per_mm)
            if side=="left":   L = max(L, px)
            if side=="right":  R = min(R, w - px)
            if side=="top":    T = max(T, px)
            if side=="bottom": B = min(B, h - px)
    if (L,T,R,B) != (0,0,w,h) and R>L and B>T:
        img = img.crop((L,T,R,B))
    return img

def autotrim_ws(img, thresh=240, pad_frac=0.04):
    arr = np.asarray(img.convert("L"))
    mask = arr < thresh
    if not mask.any(): return img
    ys,xs = np.where(mask)
    y0,y1,x0,x1 = ys.min(),ys.max(),xs.min(),xs.max()
    h,w = arr.shape
    p = int(pad_frac*max(y1-y0,x1-x0))
    return img.crop((max(0,x0-p),max(0,y0-p),min(w,x1+p),min(h,y1+p)))

def line_art_cplus(img):
    a = np.asarray(img.convert("L")).astype(np.float32)
    lo,hi,gamma = 40,230,0.90
    a = np.clip((a-lo)/(hi-lo),0,1)**gamma*255
    b = Image.fromarray(a.astype("uint8"))
    d = b.filter(ImageFilter.MinFilter(3))
    return Image.blend(b,d,0.85)

def prep_page_image(img, cfg, g):
    """Full per-image pipeline -> a raster sized to (trim + bleed) at render_dpi."""
    mode = "RGB" if cfg['color_mode']=="rgb" else "L"
    img = img.convert(mode)

    # 1. per-side crop
    img = per_side_crop(img, cfg)

    # 1b. paper-color neutralize (for yellowed/aged pages)
    # done BEFORE fit/bleed so the correction is at full source resolution,
    # and BEFORE line_art_boost so the boost sees a cleanly-white page
    paper = parse_paper_spec(cfg.get('paper_color', 'none'))
    if paper == "auto":
        paper = sample_paper_rgb(img, cfg.get('paper_sample_pct', 95))
    if paper is not None:
        # resolve target: "white" | "none" | hex | (r,g,b) tuple -> RGB triple
        raw_t = cfg.get('paper_target', 'white')
        if raw_t in (None, 'white', 'none', ''):
            target = (255, 255, 255)
        else:
            t = parse_paper_spec(raw_t)
            target = t if isinstance(t, tuple) else (255, 255, 255)
        img = neutralize_paper(img, paper, cfg.get('paper_strength', 1.0), target)

    # optional line-art boost (after crop+neutralize, before fit)
    if cfg['line_art_boost']:
        img = line_art_cplus(img).convert(mode)

    # target pixel size = trim + bleed on all sides
    px_per_mm = cfg['render_dpi']/25.4
    trim_w = cfg['page_w_mm']*px_per_mm
    trim_h = cfg['page_h_mm']*px_per_mm
    bleed_px = cfg['bleed_mm']*px_per_mm
    full_w = int(round(trim_w + 2*bleed_px))
    full_h = int(round(trim_h + 2*bleed_px))

    bg = 255 if mode=="L" else (255,255,255)

    # 2/3. fit + bleed
    if cfg['fit_mode']=="autotrim":
        img = autotrim_ws(img)
    if cfg['fit_mode'] in ("fill",):
        # cover the FULL (bleed-inclusive) box, center-crop overflow
        scale = max(full_w/img.width, full_h/img.height)
        nw,nh = int(math.ceil(img.width*scale)), int(math.ceil(img.height*scale))
        img = img.resize((nw,nh), Image.LANCZOS)
        left=(nw-full_w)//2; top=(nh-full_h)//2
        img = img.crop((left,top,left+full_w,top+full_h))
        canvas_img = img
    else:
        # "fit"/"autotrim": letterbox whole image into the TRIM area, bleed stays bg
        fit = img.copy()
        fit.thumbnail((int(trim_w),int(trim_h)), Image.LANCZOS)
        canvas_img = Image.new(mode,(full_w,full_h),bg)
        canvas_img.paste(fit, ((full_w-fit.width)//2,(full_h-fit.height)//2))
    return canvas_img, bleed_px

def to_reader(pil):
    bio=io.BytesIO(); pil.save(bio,format="PNG"); bio.seek(0)
    return ImageReader(bio)

# ===========================================================================
# DRAWING
# ===========================================================================
def draw_marks_for_page(c, x, y, g, cfg):
    """Draw the perimeter cut line (or crosses) at the TRIM boundary of one page."""
    c.saveState()
    c.setStrokeGray(cfg['mark_gray']); c.setLineWidth(0.2)
    PW,PH = g['PW'],g['PH']
    if cfg['mark_style']=="crosses":
        t=2*mm
        for (cx,cy) in [(x,y),(x+PW,y),(x,y+PH),(x+PW,y+PH)]:
            c.line(cx-t,cy,cx+t,cy); c.line(cx,cy-t,cx,cy+t)
    elif cfg['mark_style']=="perimeter_gap":
        gp=1.2*mm
        # four edges with a small gap at each corner
        c.line(x+gp,y, x+PW-gp,y); c.line(x+gp,y+PH, x+PW-gp,y+PH)
        c.line(x,y+gp, x,y+PH-gp); c.line(x+PW,y+gp, x+PW,y+PH-gp)
    else: # perimeter
        c.rect(x,y,PW,PH,stroke=1,fill=0)
    c.restoreState()

def place_image(c, reader, x, y, g, cfg):
    """Draw a page image so its TRIM box lands at (x,y,PW,PH), bleed extending outside."""
    bleed = cfg['bleed_mm']*mm
    c.drawImage(reader, x-bleed, y-bleed,
                width=g['PW']+2*bleed, height=g['PH']+2*bleed,
                preserveAspectRatio=False)

def folio_page_origins(x, y, g, cfg, which):
    """Given a folio's lower-left (x,y), return the two page trim origins.
       which='front' or 'back'. For vertical fold: left/right. Horizontal: bottom/top."""
    if cfg['fold']=="vertical":
        left  = (x,           y)
        right = (x+g['PW'],   y)
        return left, right
    else:
        bottom = (x,        y)
        top    = (x,        y+g['PH'])
        return bottom, top

def draw_fold_ticks(c, x, y, g, cfg):
    if not cfg['fold_ticks']: return
    c.saveState(); c.setStrokeGray(cfg['mark_gray']); c.setLineWidth(0.25)
    t=3*mm
    if cfg['fold']=="vertical":
        cx=x+g['PW']
        c.line(cx,y+g['FH'],cx,y+g['FH']+t); c.line(cx,y,cx,y-t)
    else:
        cy=y+g['PH']
        c.line(x,cy,x-t,cy); c.line(x+g['FW'],cy,x+g['FW']+t,cy)
    c.restoreState()

def slot_xy(idx, g):
    col = idx % g['COLS']; row = idx // g['COLS']
    x = g['OX'] + col*(g['FW']+g['GUT'])
    y = g['OY'] + (g['ROWS']-1-row)*(g['FH']+g['GUT'])
    return x,y

# ===========================================================================
# MAIN IMPOSITION
# ===========================================================================
def impose(readers_by_src, n_pages, cfg, out_path, blank_reader=None):
    g = geom(cfg)
    total, folios, n_sigs, pad = plan_signatures(
        n_pages, cfg['folios_per_sig'], cfg['pad_position'])

    per_sheet = g['PER_SHEET']
    n_sheets = math.ceil(len(folios)/per_sheet)

    c = canvas.Canvas(out_path, pagesize=(g['SW'], g['SH']))

    def get_reader(src):
        if src==0 or src not in readers_by_src:
            return blank_reader
        return readers_by_src[src]

    # we lay out front sheets then their matching backs, sheet by sheet
    for sheet in range(n_sheets):
        chunk = folios[sheet*per_sheet:(sheet+1)*per_sheet]

        # ---- FRONT ----
        c.setFont("Helvetica",6); c.setFillGray(0.5)
        c.drawString(g['MAR'], g['SH']-g['MAR']+2,
            f"IMPOSITION FRONT  sheet {sheet+1}/{n_sheets}  "
            f"{cfg['page_w_mm']}x{cfg['page_h_mm']}mm  {cfg['fold']} fold  "
            f"sig={cfg['folios_per_sig']}fol  pad={pad}blank")
        for i,f in enumerate(chunk):
            x,y = slot_xy(i,g)
            pL,pR = folio_page_origins(x,y,g,cfg,'front')
            # mirror_pages: swap which folio-half each page occupies, so pages whose
            # binding margin is on the reversed side land their gutter at the fold/spine.
            front_pairs=[(f['fL'],pL),(f['fR'],pR)]
            if cfg.get('mirror_pages'): front_pairs=[(f['fR'],pL),(f['fL'],pR)]
            for src,(px,py) in front_pairs:
                r=get_reader(src)
                if r is not None: place_image(c,r,px,py,g,cfg)
                draw_marks_for_page(c,px,py,g,cfg)
            draw_fold_ticks(c,x,y,g,cfg)
        c.showPage()

        # ---- BACK (mirrored per flip_edge) ----
        c.setFont("Helvetica",6); c.setFillGray(0.5)
        c.drawString(g['MAR'], g['SH']-g['MAR']+2,
            f"IMPOSITION BACK  sheet {sheet+1}/{n_sheets}  flip={cfg['flip_edge']} edge")
        # duplex compensation: shift back-side imposition to counter printer's
        # per-pass shift. Positive x = printer drifts back to the RIGHT -> we shift
        # back LEFT in the PDF to compensate (the printer will then push it back).
        dxo = cfg.get('duplex_offset_x_mm', 0) * mm
        dyo = cfg.get('duplex_offset_y_mm', 0) * mm
        for i,f in enumerate(chunk):
            x,y = slot_xy(i,g)
            if cfg['flip_edge']=="long":
                mx = g['SW'] - (x+g['FW']) - dxo; my = y - dyo
            else:
                mx = x - dxo; my = g['SH'] - (y+g['FH']) - dyo
            pL,pR = folio_page_origins(mx,my,g,cfg,'back')
            # back pages: bL behind fR, bR behind fL -> with the mirror, draw bL at left slot
            back_pairs=[(f['bL'],pL),(f['bR'],pR)]
            if cfg.get('mirror_pages'): back_pairs=[(f['bR'],pL),(f['bL'],pR)]
            for src,(px,py) in back_pairs:
                r=get_reader(src)
                if r is not None: place_image(c,r,px,py,g,cfg)
                draw_marks_for_page(c,px,py,g,cfg)
            draw_fold_ticks(c,mx,my,g,cfg)
        c.showPage()

    c.save()

    # ---- report ----
    print(f"== imposition complete ==")
    print(f"source pages:    {n_pages}")
    print(f"padded to:       {total}  (+{pad} blank leaves, at {cfg['pad_position']})")
    print(f"folios:          {len(folios)}   signatures: {n_sigs} x {cfg['folios_per_sig']} folios")
    print(f"grid/sheet side: {g['COLS']} cols x {g['ROWS']} rows = {per_sheet} folios")
    print(f"sheets:          {n_sheets}  (=> {n_sheets*2} printed sides)")
    print(f"page size:       {cfg['page_w_mm']}x{cfg['page_h_mm']}mm  bleed {cfg['bleed_mm']}mm  marks={cfg['mark_style']}")
    if cfg.get('paper_color', 'none') != 'none':
        print(f"paper correct:   {cfg['paper_color']} -> {cfg.get('paper_target','white')}  "
              f"strength {cfg.get('paper_strength',1.0)}")
    return total, folios, n_sigs, pad

# ===========================================================================
# SOURCE LOADERS
# ===========================================================================
def load_from_pdf(path, cfg, force_render=False, tmpdir=None):
    """Build one prepped image per PDF page, written to disk as temp files.

    Returns (readers_by_src, n_pages) where readers_by_src maps page-number -> a
    DISK PATH (not an in-memory buffer). Disk-backed so memory stays flat on large
    books (a 286-page color facsimile would OOM if all pages were held in RAM).

    force_render=False (default): EXTRACT the largest embedded image per page.
        Best for clean image-per-page PDFs (e.g. exported facsimiles).
    force_render=True (--render): RASTERIZE each whole page as it visually appears,
        ignoring embedded-image structure. Use for page-is-artwork PDFs, or pages
        with many images (saved web pages, scans w/ captions) where 'largest image'
        would grab the wrong thing.
    """
    import tempfile, os
    import pypdfium2 as pdfium
    import pypdfium2.raw as pdfium_c
    doc = pdfium.PdfDocument(path)
    g = geom(cfg)
    if tmpdir is None:
        tmpdir = tempfile.mkdtemp(prefix="impose_pages_")
    readers={}

    px_per_mm = cfg['render_dpi']/25.4
    target_long_px = max(cfg['page_w_mm'], cfg['page_h_mm']) * px_per_mm
    QUALITY = 1.4

    ext = "png"
    for idx in range(len(doc)):
        # resume: skip pages already written to the temp dir
        jpg=os.path.join(tmpdir,f"p{idx+1:04d}.jpg")
        png=os.path.join(tmpdir,f"p{idx+1:04d}.png")
        if cfg['color_mode']=="rgb" and os.path.exists(jpg):
            readers[idx+1]=jpg; continue
        if cfg['color_mode']!="rgb" and os.path.exists(png):
            readers[idx+1]=png; continue
        page=doc[idx]
        im = None
        if not force_render:
            # extract the largest embedded image (by pixel count) at native resolution
            imgs = list(page.get_objects(filter=(pdfium_c.FPDF_PAGEOBJ_IMAGE,)))
            if imgs:
                def area(obj):
                    try:
                        w, h = obj.get_px_size(); return w*h
                    except Exception:
                        return 0
                best = max(imgs, key=area)
                try:
                    im = best.get_bitmap(render=False).to_pil()
                except Exception:
                    im = None   # unusual image encoding -> fall back to rendering
        if im is None:
            # rasterize the whole page as it visually appears
            page_w_pt, page_h_pt = page.get_size()
            page_long_pt = max(page_w_pt, page_h_pt)
            want_px = target_long_px * QUALITY
            dpi = max(72, min(600, want_px / (page_long_pt/72.0)))
            im = page.render(scale=dpi/72.0).to_pil()
        page.close()
        prepped,bl=prep_page_image(im,cfg,g)
        fp=os.path.join(tmpdir,f"p{idx+1:04d}.{ext}")
        # JPEG for color to keep files small/fast; PNG for grayscale line art (lossless)
        if cfg['color_mode']=="rgb":
            fp=os.path.join(tmpdir,f"p{idx+1:04d}.jpg")
            prepped.convert("RGB").save(fp,format="JPEG",quality=92)
        else:
            prepped.save(fp,format="PNG")
        readers[idx+1]=fp                       # store PATH, not bytes
        im.close(); prepped.close()             # free promptly
    n_pages=len(doc)
    doc.close()
    return readers, n_pages

def make_blank_reader(cfg):
    g=geom(cfg)
    px_per_mm=cfg['render_dpi']/25.4
    bleed_px=cfg['bleed_mm']*px_per_mm
    full_w=int(round(cfg['page_w_mm']*px_per_mm+2*bleed_px))
    full_h=int(round(cfg['page_h_mm']*px_per_mm+2*bleed_px))
    mode="RGB" if cfg['color_mode']=="rgb" else "L"
    bg=255 if mode=="L" else (255,255,255)
    return to_reader(Image.new(mode,(full_w,full_h),bg))

# ===========================================================================
# SYNTHETIC DEMO  -- numbered, color-coded test pages
# ===========================================================================
def _load_font(size, bold=False):
    """Find a scalable font on Linux, macOS or Windows; fall back to Pillow's own."""
    candidates = (
        ["DejaVuSans-Bold.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
         "/System/Library/Fonts/Supplemental/Arial Bold.ttf", "arialbd.ttf"]
        if bold else
        ["DejaVuSans.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
         "/System/Library/Fonts/Supplemental/Arial.ttf", "arial.ttf"])
    for name in candidates:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size=size)   # Pillow >= 10.1
    except TypeError:
        return ImageFont.load_default()

def make_synthetic_readers(n_pages, cfg):
    """Each page: big number, sig/role tint, arrow to TOP, edge band to test bleed/trim."""
    g=geom(cfg)
    px_per_mm=cfg['render_dpi']/25.4
    bleed_px=int(round(cfg['bleed_mm']*px_per_mm))
    trim_w=int(round(cfg['page_w_mm']*px_per_mm))
    trim_h=int(round(cfg['page_h_mm']*px_per_mm))
    full_w=trim_w+2*bleed_px; full_h=trim_h+2*bleed_px
    readers={}
    font = _load_font(int(trim_w*0.42), bold=True)
    sfont = _load_font(int(trim_w*0.10))
    for p in range(1,n_pages+1):
        im=Image.new("RGB",(full_w,full_h),(255,255,255))
        d=ImageDraw.Draw(im)
        # bleed zone = magenta so you can SEE what gets trimmed off
        d.rectangle([0,0,full_w,full_h],fill=(255,0,255))
        # trim zone = white page
        d.rectangle([bleed_px,bleed_px,bleed_px+trim_w,bleed_px+trim_h],fill=(252,252,250))
        # thin colored inner band just inside trim, to confirm image reaches the cut line
        d.rectangle([bleed_px+4,bleed_px+4,bleed_px+trim_w-4,bleed_px+trim_h-4],
                    outline=(0,160,160),width=3)
        # big page number
        tb=d.textbbox((0,0),str(p),font=font)
        tw,th=tb[2]-tb[0],tb[3]-tb[1]
        d.text((full_w/2-tw/2-tb[0], full_h/2-th/2-tb[1]),str(p),fill=(20,20,20),font=font)
        # TOP marker
        d.text((full_w/2-12, bleed_px+8),"TOP",fill=(180,40,40),font=sfont)
        readers[p]=to_reader(im)
    return readers

def run_demo(n_pages, cfg, out_path):
    readers=make_synthetic_readers(n_pages,cfg)
    blank=make_blank_reader(cfg)
    return impose(readers,n_pages,cfg,out_path,blank_reader=blank)

# ===========================================================================
# CLI
# ===========================================================================
def parse_filename_flags(filename, base_cfg):
    """Parse settings encoded in a filename into a cfg override dict.

    Convention: fields separated by DOUBLE underscore '__'. First field is the
    book's base name; each later field is either a bare flag or a key-value pair
    joined by a single hyphen '-'.

    Bare flags:   mirror  render  gray  lineart
    Key-value:    page-30x40  sheet-330x483  folios-2  marks-perimeter
                  marks-perimeter_gap  bleed-1.2  fit-fill
                  cropL-auto  cropR-3  cropT-auto  cropB-none
                  cropL-page cropR-page cropT-page cropB-page
                        ("page" strips a black digitization matte to the paper edge;
                         use on all four sides to center the manuscript in the trim)
                  paper-auto  paper-EED9A8  paper-none
                  paperstr-0.75  (0.0 to 1.0)

    Example:
      Kells__render__page-30x40__marks-perimeter_gap.pdf
        -> render mode, 30x40 pages, perimeter_gap marks
      alfonso__mirror__page-30x40__paper-EED9A8__paperstr-0.75.pdf
        -> mirror on, 30x40 pages, paper neutralized to 75% of full white

    Returns (cfg, force_render, base_name). Unknown fields are ignored with a warning.
    """
    import os
    stem = os.path.splitext(os.path.basename(filename))[0]
    parts = stem.split("__")
    base = parts[0]
    cfg = dict(base_cfg)
    force_render = False

    for field in parts[1:]:
        f = field.strip()
        if not f:
            continue
        low = f.lower()
        # bare boolean flags
        if low == "mirror":   cfg['mirror_pages'] = True;  continue
        if low == "render":   force_render = True;         continue
        if low == "gray" or low == "grey":
            cfg['color_mode'] = "gray";                    continue
        if low == "lineart":  cfg['line_art_boost'] = True; continue
        # key-value (split on first hyphen only)
        if "-" in f:
            key, val = f.split("-", 1)
            key = key.lower()
            if key == "page":
                try: cfg['page_w_mm'], cfg['page_h_mm'] = [float(v) for v in val.lower().split("x")]
                except ValueError: print(f"  ! bad page spec '{val}' in {stem}")
            elif key == "sheet":
                try: cfg['sheet_w_mm'], cfg['sheet_h_mm'] = [float(v) for v in val.lower().split("x")]
                except ValueError: print(f"  ! bad sheet spec '{val}' in {stem}")
            elif key == "folios":
                try: cfg['folios_per_sig'] = int(val)
                except ValueError: print(f"  ! bad folios '{val}' in {stem}")
            elif key == "marks":
                if val in ("perimeter","perimeter_gap","crosses"): cfg['mark_style'] = val
                else: print(f"  ! bad marks '{val}' in {stem}")
            elif key == "bleed":
                try: cfg['bleed_mm'] = float(val)
                except ValueError: print(f"  ! bad bleed '{val}' in {stem}")
            elif key == "fit":
                if val in ("fill","fit","autotrim"): cfg['fit_mode'] = val
                else: print(f"  ! bad fit '{val}' in {stem}")
            elif key in ("cropl","cropr","cropt","cropb"):
                side = {"cropl":"crop_left","cropr":"crop_right",
                        "cropt":"crop_top","cropb":"crop_bottom"}[key]
                cfg[side] = val   # "auto" | "none" | number-as-string (handled downstream)
            elif key == "paper":
                # paper-auto | paper-none | paper-EED9A8
                cfg['paper_color'] = val
            elif key in ("target", "papertarget"):
                # target-white | target-F2E8DB
                cfg['paper_target'] = val
            elif key in ("paperstr", "paperstrength"):
                try: cfg['paper_strength'] = float(val)
                except ValueError: print(f"  ! bad paper strength '{val}' in {stem}")
            elif key in ("dxoff", "duplexoffsetx"):
                try: cfg['duplex_offset_x_mm'] = float(val)
                except ValueError: print(f"  ! bad duplex x offset '{val}' in {stem}")
            elif key in ("dyoff", "duplexoffsety"):
                try: cfg['duplex_offset_y_mm'] = float(val)
                except ValueError: print(f"  ! bad duplex y offset '{val}' in {stem}")
            else:
                print(f"  ! unknown field '{field}' in {stem} (ignored)")
        else:
            print(f"  ! unknown flag '{field}' in {stem} (ignored)")
    return cfg, force_render, base


# settings that change how a page image is PREPPED (and so what lands in the cache).
# Layout-only settings (sheet size, marks, mirror, duplex offset) aren't here:
# changing those re-imposes from the same cached pages, which is correct.
PREP_KEYS = ("page_w_mm", "page_h_mm", "bleed_mm", "color_mode", "render_dpi",
             "fit_mode", "line_art_boost", "crop_top", "crop_right", "crop_bottom",
             "crop_left", "crop_auto_thresh", "crop_auto_pad_mm", "crop_page_thresh",
             "paper_color", "paper_target", "paper_strength", "paper_sample_pct")

def prep_settings_key(cfg, force_render):
    """Short hash of the prep settings, so a changed setting gets a fresh cache
    instead of silently reusing pages prepped under the old settings."""
    import hashlib
    blob = repr([cfg.get(k) for k in PREP_KEYS] + [force_render])
    return hashlib.sha1(blob.encode()).hexdigest()[:8]

def run_folder(in_dir="process_pdfs", out_dir="output", base_cfg=None):
    """Default batch mode: impose every PDF in in_dir, writing to out_dir.

    Per-file settings come from filename-encoded flags (see parse_filename_flags).
    Each file gets its own disk-backed, resumable page cache under out_dir/.cache/,
    so large books stay memory-safe and an interrupted run resumes on re-run.
    """
    import os, glob
    if base_cfg is None: base_cfg = CFG
    here = os.path.dirname(os.path.abspath(__file__))
    in_path  = in_dir  if os.path.isabs(in_dir)  else os.path.join(here, in_dir)
    out_path = out_dir if os.path.isabs(out_dir) else os.path.join(here, out_dir)
    cache_root = os.path.join(out_path, ".cache")
    os.makedirs(in_path, exist_ok=True)
    os.makedirs(out_path, exist_ok=True)
    os.makedirs(cache_root, exist_ok=True)

    pdfs = sorted(glob.glob(os.path.join(in_path, "*.pdf")))
    if not pdfs:
        print(f"No PDFs found in: {in_path}")
        print("Drop PDFs there and run again. Encode options in the filename, e.g.:")
        print("   alfonso__mirror__page-30x40.pdf")
        print("   kells__render__page-30x40__marks-perimeter_gap.pdf")
        print("   codex__render__page-30x40__paper-EED9A8__paperstr-0.75.pdf")
        print("Flags: mirror render gray lineart | page-WxH sheet-WxH folios-N")
        print("       marks-perimeter[_gap]|crosses  bleed-N  fit-fill|fit|autotrim")
        print("       cropL/R/T/B-auto|none|N  paper-auto|none|HEX  paperstr-0..1")
        return

    print(f"Found {len(pdfs)} PDF(s) in {in_path}\n")
    for pdf in pdfs:
        name = os.path.basename(pdf)
        cfg, force_render, base = parse_filename_flags(pdf, base_cfg)
        out_file = os.path.join(out_path, f"{base}__imposed.pdf")
        cache_dir = os.path.join(cache_root, f"{base}_{prep_settings_key(cfg, force_render)}")
        os.makedirs(cache_dir, exist_ok=True)
        print(f"--- {name}")
        print(f"    page={cfg['page_w_mm']}x{cfg['page_h_mm']}mm sheet={cfg['sheet_w_mm']}x{cfg['sheet_h_mm']}mm "
              f"folios={cfg['folios_per_sig']} marks={cfg['mark_style']} "
              f"render={force_render} mirror={cfg.get('mirror_pages',False)} "
              f"color={cfg['color_mode']} fit={cfg['fit_mode']} "
              f"paper={cfg.get('paper_color','none')}/{cfg.get('paper_strength',1.0)}")
        try:
            readers, n = load_from_pdf(pdf, cfg, force_render=force_render, tmpdir=cache_dir)
            blank = make_blank_reader(cfg)
            impose(readers, n, cfg, out_file, blank_reader=blank)
            print(f"    -> {out_file}\n")
        except Exception as e:
            print(f"    !! FAILED: {e}\n")
    print(f"Done. Imposed files in: {out_path}")
    print("(page caches kept under .cache/ for fast re-runs; delete to reclaim space)")


if __name__=="__main__":
    ap=argparse.ArgumentParser(description="Parametric nested-saddle imposition")
    ap.add_argument("--pdf",help="source PDF (one image per page)")
    ap.add_argument("--demo",type=int,metavar="N",help="run synthetic demo with N pages")
    ap.add_argument("--out",default="imposed.pdf")
    # a few common overrides from CLI
    ap.add_argument("--page",help="WxH in mm, e.g. 30x40")
    ap.add_argument("--folios",type=int)
    ap.add_argument("--sheet",help="WxH in mm, e.g. 330x483")
    ap.add_argument("--marks",choices=["perimeter","perimeter_gap","crosses"])
    ap.add_argument("--bleed",type=float)
    ap.add_argument("--gray",action="store_true",help="grayscale mode")
    ap.add_argument("--lineart",action="store_true",help="apply line-art boost")
    ap.add_argument("--fit",choices=["fill","fit","autotrim"])
    ap.add_argument("--cropL"); ap.add_argument("--cropR")
    ap.add_argument("--cropT"); ap.add_argument("--cropB")
    ap.add_argument("--paper",
                    help="paper color for yellowed-page correction: 'auto', 'none', "
                         "or hex like EED9A8 (with or without '#')")
    ap.add_argument("--paper-target", dest="paper_target",
                    help="what paper should become: 'white' (default, bleach) or a "
                         "hex like F2E8DB (preserves warm cream character)")
    ap.add_argument("--paper-strength", type=float, dest="paper_strength",
                    help="0.0 (no correction) .. 1.0 (paper -> target exactly); "
                         "0.7 is a good starting point for aged manuscripts")
    ap.add_argument("--render",action="store_true",
                    help="rasterize each whole PDF page (ignore embedded-image structure); "
                         "use for page-is-artwork PDFs or pages with many images")
    ap.add_argument("--tmpdir",help="reuse/resume a page-cache directory")
    ap.add_argument("--prep-only",action="store_true",
                    help="only render/prep pages to the cache dir (resumable), don't impose")
    ap.add_argument("--mirror",action="store_true",
                    help="swap page-within-folio placement so gutters land at the spine "
                         "(use when source pages' binding margin is on the reversed side)")
    ap.add_argument("--check",type=int,metavar="N",
                    help="just report folios/sheets/waste for N pages and exit (no file)")
    ap.add_argument("--indir",default="process_pdfs",
                    help="folder to batch-process when no --pdf/--demo given (default process_pdfs)")
    ap.add_argument("--outdir",default="output",
                    help="output folder for batch mode (default output)")
    a=ap.parse_args()

    cfg=dict(CFG)
    if a.page: cfg['page_w_mm'],cfg['page_h_mm']=[float(v) for v in a.page.lower().split("x")]
    if a.sheet: cfg['sheet_w_mm'],cfg['sheet_h_mm']=[float(v) for v in a.sheet.lower().split("x")]
    if a.folios: cfg['folios_per_sig']=a.folios
    if a.marks: cfg['mark_style']=a.marks
    if a.bleed is not None: cfg['bleed_mm']=a.bleed
    if a.gray: cfg['color_mode']="gray"
    if a.lineart: cfg['line_art_boost']=True
    if a.fit: cfg['fit_mode']=a.fit
    if a.mirror: cfg['mirror_pages']=True
    if a.paper is not None: cfg['paper_color']=a.paper
    if a.paper_target is not None: cfg['paper_target']=a.paper_target
    if a.paper_strength is not None: cfg['paper_strength']=a.paper_strength
    for k,attr in [("crop_left","cropL"),("crop_right","cropR"),
                   ("crop_top","cropT"),("crop_bottom","cropB")]:
        v=getattr(a,attr)
        if v is not None: cfg[k]=v

    if a.check is not None:
        # dry-run capacity/waste report, no rendering
        g=geom(cfg)
        total,folios,n_sigs,pad=plan_signatures(a.check,cfg['folios_per_sig'],cfg['pad_position'])
        per_sheet=g['PER_SHEET']; n_sheets=math.ceil(len(folios)/per_sheet)
        last=len(folios)-(n_sheets-1)*per_sheet
        waste=(per_sheet-last)/per_sheet*100 if n_sheets>0 else 0
        print(f"content pages:   {a.check}")
        print(f"padded to:       {total}  (+{pad} blank leaves)")
        print(f"folios:          {len(folios)}   signatures: {n_sigs} x {cfg['folios_per_sig']}")
        print(f"folios/sheet:    {per_sheet}  ({g['COLS']}x{g['ROWS']})  at {cfg['page_w_mm']}x{cfg['page_h_mm']}mm on {cfg['sheet_w_mm']}x{cfg['sheet_h_mm']}mm")
        print(f"sheets needed:   {n_sheets}")
        if n_sheets>1:
            print(f"last sheet uses: {last}/{per_sheet} folios  ({waste:.0f}% of last sheet wasted)")
            if waste>=50:
                cap=per_sheet*4
                print(f"   ! WASTEFUL: last sheet mostly empty. A single sheet holds {cap} pages;")
                print(f"     trim to <= {cap} content pages to stay on one sheet.")
        else:
            cap=per_sheet*4
            print(f"single sheet OK (capacity {cap} pages, {cap-total} pages of headroom)")
        sys.exit(0)

    if a.demo:
        run_demo(a.demo,cfg,a.out)
    elif a.pdf:
        readers,n=load_from_pdf(a.pdf,cfg,force_render=a.render,tmpdir=a.tmpdir)
        if a.prep_only:
            import os
            anydir=a.tmpdir or os.path.dirname(next(iter(readers.values())))
            print(f"prepped {len(readers)} pages into: {anydir}")
        else:
            blank=make_blank_reader(cfg)
            impose(readers,n,cfg,a.out,blank_reader=blank)
    else:
        # DEFAULT: batch-process every PDF in ./process_pdfs -> ./output
        # (this is what runs when you just press Run in PyCharm with no arguments).
        # CLI flags given here become the *base* config; per-file filename flags override.
        run_folder(in_dir=a.indir, out_dir=a.outdir, base_cfg=cfg)