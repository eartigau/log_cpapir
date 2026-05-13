# CFHT CPAPIR Observations Dashboard

A clean, interactive dashboard for exploring reduced CPAPIR observations from `reductions/`.

## Repository

- GitHub user: `eartigau`
- Repository: `log_cpapir`
- HTTPS URL: `https://github.com/eartigau/log_cpapir.git`

Clone and enter the project:

```bash
git clone git@github.com:eartigau/log_cpapir.git
cd log_cpapir
```

For headless servers (no GUI), SSH is strongly recommended to avoid `gnome-ssh-askpass` issues.

If you must use HTTPS, use a Personal Access Token (PAT) instead of a password:

```bash
git clone https://github.com/eartigau/log_cpapir.git
# Username: your GitHub username
# Password: your GitHub PAT (not your account password)
```

## Overview

Current dashboard features include:

- **Hierarchical browsing** by year and night
- **Interactive Bokeh plots** for FWHM and ZP1S
- **Photometric `phot_*` files included in quality plots** (not listed in the main reduced-files table)
- **Science image popup** (PNG preview with celestial overlays and magnifier)
- **PSF popup** from native `*_PSF.png` files
- **Filter ordering by physical wavelength** (not alphabetic), with I/J/H/K in italics in UI
- **Header cache CSV** for fast reruns on large archives
- **User-aware paths and optional web sync** (auto-sync for `cpapir`, optional `--no-sync`)

## Files

- `generate_dashboard.py` — Dashboard generator and optional rsync sync logic
- `dashboard_config.yaml` — Shared config (paths, sync, excludes)
- `dashboard_config.local.yaml` — Optional local override (ignored by git)
- `dashboard.html` — Generated dashboard
- `dashboard_assets/` — Generated previews and `header_cache.csv`

## Usage

### Generate/Regenerate the Dashboard

To create or update the dashboard with the latest observations:

```bash
python generate_dashboard.py
```

This will:
1. Scan configured `reductions/` nights
2. Reuse/update FITS header cache (`dashboard_assets/header_cache.csv`)
3. Build/update PNG previews
4. Build/update `dashboard.html`
5. Run optional rsync sync when enabled for the current user

Run without sync (useful for tests):

```bash
python generate_dashboard.py --no-sync
```

### View the Dashboard

Open `dashboard.html` in any modern web browser:

```bash
open dashboard.html
```

Or use the file path directly:
```
file:///Users/eartigau/GitHubProjects/log_cpapir/dashboard.html
```

## Dashboard Sections

### 1. Year/Night Browser
Collapsible sections by year and by observing night.

### 2. Night Quality Plots
Two interactive plots per night:
- FWHM vs time
- ZP1S vs time

### 3. Observation Table
Detailed table of all observations, organized by observation night:
- **Target**: Astronomical target name
- **Filter**: Observation filter (J, H, I, HeI, etc.)
- **Date-Obs**: ISO 8601 timestamp of observation start
- **Exptime (s)**: Integration time in seconds
- **RA (°)**: Right Ascension coordinate in degrees
- **Dec (°)**: Declination coordinate in degrees
- **Apercu**: Telescope icon opens science PNG popup with magnifier
- **Carte PSF**: Star icon opens PSF popup
- **File**: FITS filename

## Data Sources

The dashboard reads metadata from:

1. **Science FITS files** (`*_J.fits`, `*_H.fits`, `*_I.fits`, `*_HeI.fits`, etc.)
2. **Photometric files** (`phot*.fits`, `phot*.fits.gz`, `*phot*.fits*`) for plot enrichment
3. **PSF PNG files** (`*_PSF.png`) for PSF popups

Header keys used include `DATE-OBS`, `TEXP/EXPTIME`, `FILTER`, `OBJECT`, `RA`, `DEC`, `FWHM`, `ZP1S`.

## Customization

To modify behavior:

1. Edit `dashboard_config.yaml` for paths, sync target, excludes
2. Edit `generate_dashboard.py` for UI/plots/logic

Common modifications:
- change filter wavelengths/colors
- adjust preview rendering options
- tune sync excludes

3. Regenerate the dashboard:
   ```bash
   python generate_dashboard.py
   ```

## Technical Details

### Python Dependencies

`astropy`, `pandas`, `matplotlib`, `numpy`, `bokeh`, `pyyaml`

Install with:
```bash
pip install astropy pandas matplotlib numpy bokeh pyyaml
```

### Output Format

- Dashboard: HTML + referenced preview images
- File size depends on archive depth and preview count
- Compatibility: All modern web browsers (Chrome, Firefox, Safari, Edge)

### Performance

- First run reads headers and builds cache
- Next runs are faster via `dashboard_assets/header_cache.csv`
- Sync step only runs for configured users unless `--no-sync`

## Notes

- The script discovers nights in 6-digit directories (YYMMDD)
- PSF FITS are not used for table rows; PSF PNGs are used for PSF popup
- Missing/invalid headers are skipped
- For local tests, use `--no-sync`

## Future Enhancements

Potential additions:
- Optional CLI to force sync
- Incremental preview regeneration policy by age/size
- Dedicated nightly report exports

## License

See repository for license information.
