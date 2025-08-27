Place the FULL ExifTool download in this folder.

Windows (recommended):
1) Download the Windows package from https://exiftool.org/ (zip).
2) Unzip it.
   - If you see **exiftool(-k).exe**, rename it to **exiftool.exe**.
3) Copy **ALL** extracted contents into this `tools/` folder so the layout becomes:

tools/
  exiftool.exe            <-- must be here (top-level)
  exiftool_files/         <-- if present in your download

Alternative layout that also works:
tools/
  exiftool_files/
    exiftool.exe
    exiftool_files/
    ...

macOS / Linux:
- Install `exiftool` via your package manager (brew/apt) or download the tarball.
- Ensure `exiftool` is on your PATH **or** place the binary in this `tools/` folder.

This project does **not** ship third‑party binaries.
