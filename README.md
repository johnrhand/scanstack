# ScanStack

Turns a stack of scanned pages into one PDF on US Letter pages, without the
Preview → Print → Save as PDF routine.

## Using it

1. Drop scans or a folder onto the window (or the Dock icon). Pages go in
   name order, so `scan 2` comes before `scan 10`. Multi-page TIFFs become
   several pages.
2. Check the order. Drag thumbnails to move them. ⌘[ and ⌘] rotate, and
   Delete removes a page.
3. Pick a look and a size, watch the estimate, and press **Save PDF…** (⌘S).

**Merge Fronts/Backs** is for double-sided originals on a one-sided feeder.
Scan all the fronts, flip the stack over, and scan all the backs. It then
reorders them as front 1, back 1, front 2, back 2… **Sort A→Z** undoes it.

## Making files smaller

These are listed from the biggest savings to the smallest:

- **Black & white** stores pure black on white in the fax format (CCITT G4).
  Text pages are often 10–20× smaller than greyscale. Don't use it for photos.
- **Resolution**: 150 dpi is fine for reading on screen and 200 prints well.
  Pages are never stored at more detail than the scan actually has.
- **Automatic colour** stores pages with no real colour as greyscale.
- **JPEG quality**: settings around 60–75 are hard to tell apart from the original.
- **Clean up paper background** turns grey or yellowed paper white. It's
  mostly about looks, and saves a little space.

Your scans are never changed. The PDF is written to a temporary file and
moved into place only when it's complete.

## Installing on the Mac

1. Push this folder to a GitHub repo. If you upload through the website,
   press ⇧⌘. in Finder first so the hidden `.github` folder shows and gets included.
2. Wait for the Actions run (about 5–10 minutes) and download the `.dmg`
   from the run's Artifacts.
3. Drag ScanStack to Applications. The first time, right-click it and choose
   Open, or run:
   `xattr -dr com.apple.quarantine /Applications/ScanStack.app`

To run from source instead: `pip install -r requirements.txt` then `python3 main.py`.
