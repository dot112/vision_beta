# Line setup: small camera cards and a page per camera

Date: 2026-11-01. Status: design agreed with the owner, waiting for the spec review.

## Why

Today every camera card on Line setup holds all of its settings in folds (Camera, Job, AI model and classes,
Counting, Codes, Text fields, Image, Video). With two or three cameras the page gets long and the cards
uneven. The owner wants each card to show only the camera, and its settings on a page of their own.

## What the owner asked for (2026-11-01)

- Each camera card on Line setup shows just the camera, with **Settings** and **Delete** buttons.
- **Settings** opens a new page inside Line setup for that camera, with its settings split into pages
  (Camera, Job, Counting, ... one page per group of settings).
- Image and video settings are on **one** page.
- Chosen in the questions: the card shows the live picture, name, job and a one-line summary, with the
  move arrows kept; the camera page has a menu on the left (live picture above it), the chosen page on the
  right, and a top bar with Back and Save line setup.

## The small camera card

```
┌──────────────────────────────┐
│ ● PHONE IP CAM  [TEXT]  ‹ ›  │   status dot, name, job badge, move earlier / later
│ Camera 2 · IP · One picture  │   summary line
│   per trigger                │
│ ┌──────────────────────────┐ │
│ │       live picture       │ │
│ └──────────────────────────┘ │
│ [ ⚙ Settings ]   [ 🗑 Delete ] │
└──────────────────────────────┘
```

- **Summary line:** `Camera N · <type> · <what it does>`:
  - vision jobs (counting, station, with or without codes): "Count lines" or "One picture per trigger";
    a station joined to the product result adds "· joined";
  - code reader: "Shows codes" (Continuously), "Reads on wire line 1", "Reads on wire line 2" or
    "Reads when an input asks";
  - text reader: "Shows text" (Continuously) or "One picture per trigger".
- A camera the version 9 upgrade flagged (`qr_trigger_review`) shows a short warning line on the card:
  "Choose when it reads codes (Settings → Codes)".
- **Settings** is open at every level: Level 1 sees the settings greyed out, as now. It has no `data-lock`,
  so it also works while the configuration is locked.
- **Delete** is today's remove button: Level 2, `data-lock`, and the same confirm dialog (the camera stays
  on the Cameras page; its settings on this line are dropped when the line is saved).
- The move arrows point left and right (the cards are in a grid) and do what Move up / Move down do now.

## The camera page

```
← Line 1    ● PHONE IP CAM [TEXT]          Unsaved changes · [Save line setup]
┌────────────────────┬──────────────────────────────────────────────┐
│ ┌────────────────┐ │  Reading                                     │
│ │  live picture  │ │  Count mode  [One picture per trigger ▾]     │
│ └────────────────┘ │  Picture taken by [System trigger ▾]         │
│  Camera            │  ...                                         │
│  Job               │                                              │
│ ▸ Reading          │                                              │
│  Text fields       │                                              │
│  Image and video   │                                              │
└────────────────────┴──────────────────────────────────────────────┘
```

- It opens **inside Line setup** and takes the whole tab while it is open: the line panel, the other camera
  cards, the Add camera card, Product checks, Inputs, Send results and Output actions are hidden (they stay
  on the page, so the one Save still reads them).
- **Top bar:** "← <line name>" back to the cards, the camera's status dot, name and job badge,
  "Unsaved changes" when there are any, and **Save line setup** (Level 2, `data-lock`).
- **Left:** the camera's live picture, then the menu. **Right:** the chosen page with its title.
- **The pages**, in this order, each only when it fits the camera's job (as the folds are shown today):

  | Page | Shown for | Holds |
  |---|---|---|
  | Camera | every job | Device, Name, Connect |
  | Job | every job | What this camera does, station / joined settings |
  | AI model and classes | vision jobs | model, products, defects, confidence, name-based defects |
  | Counting ("Reading" for a text reader) | vision jobs, text reader | count mode, picture settings, count lines, tracking |
  | Codes | jobs that read codes | code type, read codes, action, list, batch, no read |
  | Text fields | text reader | the text reader's fields, picture, Read |
  | Image and video | every job | the Image settings, then the Video settings |

- Every page holds exactly today's fields with today's notes: nothing is removed, renamed or moved between
  pages, except that Image and Video are one page.
- Changing the job updates the menu at once. When the page shown no longer fits the job, the Job page shows.
- **Phone width:** the picture goes on top, the menu becomes a row of tabs that scrolls sideways, and the
  page is below it. No sideways scroll of the whole page.

## How it behaves

- **Back** returns to the cards and keeps every edit. One **Save line setup** saves the line and all its
  cameras, from the camera page or from the line panel, as today.
- **Saving on a camera page stays on it**: the page is drawn again after the save (as Line setup is today)
  and the same camera's page opens at the same page. The open camera page is also kept when you leave the
  Line setup tab and come back on the same line; switching to another line shows that line's cards.
- **The last page per camera** is remembered in this browser (local storage; the remembered open folds go).
  A camera opened for the first time shows the Camera page.
- **Adding a camera** opens its page at once, at what needs filling in first: AI model and classes for a
  vision camera, Codes for a code reader, Text fields for a text reader.
- **When Save stops on a camera** (no vision model, no product list, a text field without a format or YOLO
  model), the toast is shown as now and that camera's page opens at the page with the problem.
- **Settings on the Cameras page**, for a camera on a line, opens that camera's page at Image and video
  (today it opens the card's Camera and Image folds).
- **Live pictures:** on the cards every camera's live picture runs, as now; on a camera page only that
  camera's runs. The text reader's Live preview and the count-line and image pictures load when their page
  is shown, as they load today when their fold opens. The text reader is told when its page is shown or
  hidden (`TextReader.wake`), since it no longer gets a fold click.

## How it is built

The settings stay where they are: each camera keeps one element with all its fields, and only the way it
is shown changes. So the save (`cardEntry`, `saveLineSetup`), the checks (`syncCard`), Unsaved changes, the
configuration lock and the text reader keep working on the same fields.

- **`assets/camera_pages.js` (new):** the pure parts, exported as `window.CameraPages` and
  `module.exports` like `text_reader.js`:
  - `PAGES`: the pages in order, with their titles;
  - `pagesFor(kind)`: the pages a job shows (from whether it is a vision job, reads codes, or reads text);
  - `summary(...)`: the card's summary line;
  - `menuHtml(...)`: the menu's markup;
  - `pageForProblem(problem)`: the page a Save problem opens.
- **`assets/production_lines.js`:**
  - the card's markup: head (dot, name, badge, arrows; Back, Unsaved and Save in page mode), summary line,
    warning line, live picture, the menu, the page sections, and the Settings and Delete buttons;
  - the page sections are today's fold sections without their fold buttons. They keep their class and
    `data-fold` names, so the text reader and the tests keep their hooks; `open` now means "the page
    shown". The Image and Video sections become one section, `data-fold="image"`, titled "Image and video";
  - `openCameraPage(card, page)`, `showCameraPage(card, page)` and `closeCameraPage()`; `setup.openCamera`
    holds the open camera and page so a redraw (after Save, coming back to the tab) opens it again;
  - `syncCard` fills the summary line and shows the menu entries that fit the job;
  - the add, Save problem and Cameras page Settings paths open the camera page as above;
  - the styles for the small card, page mode and phone width, in the same style block as the other
    `.pl-` styles.
- **`assets/text_reader.js`:** drops its fold-button listener; production_lines calls `TextReader.wake`
  when a page is shown or hidden.
- **`dashboard.html`:** the `camera_pages.js` script tag and new `?v=` numbers.
- No server change.

## Testing

- **node tests** for `camera_pages.js`: the pages each job shows, the summary line for each job and count
  mode, the page each Save problem opens, the menu markup.
- **Markup tests** for production_lines.js and dashboard.html: Settings and Delete on the card (Delete
  locked and Level 2, Settings not locked), no fold buttons left, one Image and video page, the script tag;
  the tests that check the old fold markup are updated.
- **The whole test suite** passes, apart from the known Windows-only failures.
- **A live check on a scratch copy** of the data (never the real `data/`, never the owner's server on 8000):
  open Line 1's cards and each camera's page, change the job and watch the menu, save from a camera page and
  stay on it, add a camera, a Save problem opening its page, the Cameras page Settings link, Level 1 and the
  configuration lock; screenshots at desktop and phone width.

## Not in this change

- No change to what any setting does, to the server, or to Inputs, Send results and Output actions.
- No browser Back-button history for the camera page (the dashboard has no page history today).
