#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Genere un tableau de bord HTML interactif a partir des observations reduites CPAPIR.
- Panneaux compactes par annee puis par date
- Graphiques interactifs Bokeh embarques dans le HTML
- Apercus JPG compresses generes depuis les FITS
"""

import sys
import json
import yaml
import colorsys
import getpass
import os
import argparse
import subprocess
import gc
from datetime import datetime
from pathlib import Path

# Force UTF-8 output encoding (for legacy servers)
if sys.stdout.encoding != 'utf-8':
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

import numpy as np
import pandas as pd
from tqdm import tqdm
from astropy.io import fits
from astropy.wcs import WCS, FITSFixedWarning
from astropy.wcs.utils import proj_plane_pixel_scales
import astropy.units as u
import warnings

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

matplotlib.rcParams['figure.max_open_warning'] = 0  # Disable figure limit warning

warnings.filterwarnings('ignore', category=FITSFixedWarning)

from bokeh.embed import components
from bokeh.layouts import row
from bokeh.models import ColumnDataSource, HoverTool
from bokeh.plotting import figure
from bokeh.resources import INLINE


# Helper function for emoji/fallback messages
EMOJI_MAP = {
    '📡': '[SCAN]',
    '📂': '[DIR]',
    '🌟': '[PSF]',
    '✅': '[OK]',
    '📊': '[PLOT]',
    '✨': '[DONE]',
}

def emoji_print(*args, **kwargs):
    """Print with emoji fallback for legacy servers."""
    try:
        print(*args, **kwargs)
    except (UnicodeEncodeError, UnicodeDecodeError):
        # Fallback: replace emojis with text
        text = ' '.join(str(arg) for arg in args)
        for emoji, fallback in EMOJI_MAP.items():
            text = text.replace(emoji, fallback)
        print(text, **kwargs)


# Chargement config YAML (base versionnee + override locale optionnelle)
CONFIG_PATH = Path('dashboard_config.yaml')
LOCAL_CONFIG_PATH = Path('dashboard_config.local.yaml')

config = {}
if CONFIG_PATH.exists():
    with open(CONFIG_PATH, 'r') as f:
        config = yaml.safe_load(f) or {}

if LOCAL_CONFIG_PATH.exists():
    with open(LOCAL_CONFIG_PATH, 'r') as f:
        local_config = yaml.safe_load(f) or {}
    config.update(local_config)

current_user = getpass.getuser()
path_by_user = config.get('reductions_path_by_user', {}) or {}
assets_by_user = config.get('assets_path_by_user', {}) or {}

# Priorite: mapping par usager -> reductions_path explicite -> fallback relatif au dossier courant.
reductions_cfg = path_by_user.get(current_user, config.get('reductions_path', './reductions'))
REDUCTIONS_PATH = Path(reductions_cfg).expanduser()
if not REDUCTIONS_PATH.is_absolute():
    REDUCTIONS_PATH = (Path.cwd() / REDUCTIONS_PATH).resolve()

PROJECT_ROOT = REDUCTIONS_PATH.parent

output_cfg = Path(str(config.get('output_path', './dashboard.html'))).expanduser()
if output_cfg.is_absolute():
    OUTPUT_PATH = output_cfg
else:
    OUTPUT_PATH = (PROJECT_ROOT / output_cfg).resolve()

REGENERATE_PREVIEWS = config.get('regenerate_previews', True)

assets_cfg = assets_by_user.get(current_user, config.get('assets_path', './dashboard_assets'))
ASSETS_PATH = Path(str(assets_cfg)).expanduser()
if not ASSETS_PATH.is_absolute():
    ASSETS_PATH = (Path.cwd() / ASSETS_PATH).resolve()

# Les artefacts dashboard (previews + cache) restent hors de reductions.
HEADER_CACHE_PATH = ASSETS_PATH / 'header_cache.csv'

SYNC_ENABLED_USERS = set(config.get('sync_enabled_users', ['cpapir']))
SYNC_TARGET_BY_USER = config.get('sync_to_web_target_by_user', {}) or {}
SYNC_PORT_BY_USER = config.get('sync_port_by_user', {}) or {}
SYNC_TARGET = str(SYNC_TARGET_BY_USER.get(current_user, config.get('sync_to_web_target', ''))).strip()
SYNC_PORT = int(SYNC_PORT_BY_USER.get(current_user, config.get('sync_port', 22)))
SYNC_EXCLUDES = config.get('sync_excludes', [
    '*phot*fits*',
    '*.fits',
    '*.fits.gz',
    '*.dat',
    '*.sav',
    '*.csv',
])
SYNC_ENABLED = (current_user in SYNC_ENABLED_USERS) and bool(SYNC_TARGET)


def to_web_path(path_obj):
    """Retourne un chemin relatif au dossier du HTML pour les liens img/src."""
    return os.path.relpath(path_obj, OUTPUT_PATH.parent).replace('\\', '/')


def run_rsync(source, dest, excludes=None):
    """Execute un rsync avec ssh port configurable."""
    cmd = ['rsync', '-av', '-e', f'ssh -oPort={SYNC_PORT}']
    for ex in (excludes or []):
        cmd.append(f'--exclude={ex}')
    cmd.extend([str(source), str(dest)])
    print('RSYNC:', ' '.join(cmd))
    result = subprocess.run(cmd, check=False)
    return result.returncode == 0


def sync_web_archive(no_sync=False):
    """Synchronise le tableau de bord et les dossiers de nuits vers le serveur web (mode cpapir)."""
    if no_sync:
        print('Sync web desactive par option --no-sync')
        return

    if not SYNC_ENABLED:
        print(f'Sync web desactive (user={current_user}, target="{SYNC_TARGET}")')
        return

    print(f'Sync web active: {SYNC_TARGET} (port {SYNC_PORT})')

    # 1) Sync du tableau de bord principal et des assets web.
    ok_main = run_rsync(OUTPUT_PATH, SYNC_TARGET)
    ok_assets = run_rsync(ASSETS_PATH, os.path.join(SYNC_TARGET, 'dashboard_assets/'))

    # 2) Sync des nuits en conservant les exclusions lourdes (fits/csv/etc.).
    nights = sorted(REDUCTIONS_PATH.glob('[0-9][0-9][0-9][0-9][0-9][0-9]'))
    ok_nights = True
    for night_dir in nights:
        if not night_dir.is_dir():
            continue
        if not run_rsync(night_dir, os.path.join(SYNC_TARGET, 'reductions/'), excludes=SYNC_EXCLUDES):
            ok_nights = False

    # 3) Sync des pages HTML de nuits, si elles existent.
    night_html_files = sorted(REDUCTIONS_PATH.glob('[0-9][0-9][0-9][0-9][0-9][0-9].html'))
    ok_html = True
    for night_html in night_html_files:
        if not run_rsync(night_html, os.path.join(SYNC_TARGET, 'reductions/')):
            ok_html = False

    if ok_main and ok_assets and ok_nights and ok_html:
        print('Sync web terminee avec succes.')
    else:
        print('Sync web terminee avec erreurs (voir les lignes RSYNC ci-dessus).')

# Longueurs d'onde centrales (um) d'apres le guide CPAPIR de l'OMM.
FILTER_WAVELENGTH_UM = {
    'I': 0.85,
    'J': 1.25,
    'PaB': 1.2814,
    'Paβ': 1.2814,
    'CH4': 1.57,
    'H': 1.65,
    'CONT2': 2.033,
    'HeI': 2.062,
    'CIV': 2.081,
    'H2': 2.122,
    'K': 2.15,
    'Ks': 2.15,
    'BrG': 2.165,
    'Brγ': 2.165,
    'HeII': 2.192,
    'CONT1': 2.255,
}


def filter_sort_key(filter_name):
    """Trie les bandes par longueur d'onde, puis alphabetiquement en secours."""
    f = str(filter_name)
    return (FILTER_WAVELENGTH_UM.get(f, 99.0), f)


def filter_color(filter_name):
    """Retourne une couleur selon la longueur d'onde (bleu->rouge, puis tons rougeatres au-dela de K)."""
    f = str(filter_name)
    wl = FILTER_WAVELENGTH_UM.get(f)
    if wl is None:
        return '#888888'

    wl_i = FILTER_WAVELENGTH_UM['I']
    wl_k = FILTER_WAVELENGTH_UM['K']

    # Entre I et K: de bleu (I) vers rouge (K).
    if wl <= wl_k:
        t = (wl - wl_i) / (wl_k - wl_i)
        t = max(0.0, min(1.0, t))
        hue = (220.0 * (1.0 - t) + 2.0 * t) / 360.0
        sat = 0.78
        val = 0.93
    else:
        # Au-dela de K: rester dans les teintes rougeatres.
        t = min(1.0, (wl - wl_k) / 0.15)
        hue = (2.0 + 10.0 * t) / 360.0
        sat = 0.80 - 0.15 * t
        val = 0.92 - 0.10 * t

    r, g, b = colorsys.hsv_to_rgb(hue, sat, val)
    return f'#{int(r * 255):02x}{int(g * 255):02x}{int(b * 255):02x}'


def format_filter_html(filter_name):
    """Affiche I/J/H/K en italique dans l'interface HTML."""
    f = str(filter_name)
    if f in {'I', 'J', 'H', 'K'}:
        return f'<i>{f}</i>'
    return f


def parse_nightid(nightid):
    """Convertit un NIGHTID YYMMDD en datetime + etiquettes."""
    try:
        yy = int(nightid[0:2])
        mm = int(nightid[2:4])
        dd = int(nightid[4:6])
        year = 2000 + yy
        dt = datetime(year, mm, dd)
        return dt, str(year), dt.strftime('%Y-%m-%d')
    except Exception:
        return None, 'Inconnu', nightid


def read_fits_headers(fits_file):
    """Lit les mots-clef utiles depuis un FITS."""
    try:
        with fits.open(fits_file) as hdul:
            h = hdul[0].header
            data = hdul[0].data if len(hdul) > 0 else None

            exptime = h.get('TEXP', h.get('EXPTIME', 0))
            try:
                exptime = float(exptime)
            except (ValueError, TypeError):
                exptime = 0.0

            try:
                fwhm = float(h.get('FWHM', np.nan))
            except (ValueError, TypeError):
                fwhm = np.nan

            try:
                zp1s = float(h.get('ZP1S', np.nan))
            except (ValueError, TypeError):
                zp1s = np.nan

            return {
                'date_obs': h.get('DATE-OBS', ''),
                'exptime': exptime,
                'filter': h.get('FILTER', 'N/A'),
                'object': h.get('OBJECT', 'N/A'),
                'ra': float(h.get('RA', 0)) if 'RA' in h else 0.0,
                'dec': float(h.get('DEC', 0)) if 'DEC' in h else 0.0,
                'shape': data.shape if data is not None else (0, 0),
                'fwhm': fwhm,
                'zp1s': zp1s,
            }
    except Exception:
        return None


def _to_float_or_nan(value):
    try:
        if pd.isna(value):
            return np.nan
        return float(value)
    except Exception:
        return np.nan


def _shape_from_cache(v0, v1):
    s0 = int(v0) if pd.notna(v0) else 0
    s1 = int(v1) if pd.notna(v1) else 0
    return (s0, s1)


def load_header_cache(cache_path):
    """Charge le cache CSV des entetes FITS."""
    if not cache_path.exists():
        return {}

    try:
        df = pd.read_csv(cache_path)
    except Exception:
        return {}

    cache = {}
    for _, row in df.iterrows():
        rel_path = str(row.get('rel_path', '')).strip()
        if not rel_path:
            continue

        cache[rel_path] = {
            'mtime': _to_float_or_nan(row.get('mtime')),
            'size': int(row.get('size', 0)) if pd.notna(row.get('size')) else 0,
            'header': {
                'date_obs': '' if pd.isna(row.get('date_obs')) else str(row.get('date_obs')),
                'exptime': _to_float_or_nan(row.get('exptime')),
                'filter': 'N/A' if pd.isna(row.get('filter')) else str(row.get('filter')),
                'object': 'N/A' if pd.isna(row.get('object')) else str(row.get('object')),
                'ra': _to_float_or_nan(row.get('ra')),
                'dec': _to_float_or_nan(row.get('dec')),
                'shape': _shape_from_cache(row.get('shape0'), row.get('shape1')),
                'fwhm': _to_float_or_nan(row.get('fwhm')),
                'zp1s': _to_float_or_nan(row.get('zp1s')),
            },
        }

    return cache


def save_header_cache(cache_path, cache):
    """Sauvegarde le cache CSV des entetes FITS."""
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    for rel_path, entry in cache.items():
        h = entry.get('header', {}) or {}
        shape = h.get('shape', (0, 0))
        s0 = int(shape[0]) if len(shape) > 0 else 0
        s1 = int(shape[1]) if len(shape) > 1 else 0
        rows.append({
            'rel_path': rel_path,
            'mtime': entry.get('mtime', np.nan),
            'size': entry.get('size', 0),
            'date_obs': h.get('date_obs', ''),
            'exptime': h.get('exptime', np.nan),
            'filter': h.get('filter', 'N/A'),
            'object': h.get('object', 'N/A'),
            'ra': h.get('ra', np.nan),
            'dec': h.get('dec', np.nan),
            'shape0': s0,
            'shape1': s1,
            'fwhm': h.get('fwhm', np.nan),
            'zp1s': h.get('zp1s', np.nan),
        })

    pd.DataFrame(rows).sort_values('rel_path').to_csv(cache_path, index=False)


def read_fits_headers_cached(fits_file, cache):
    """Lit les entetes via cache CSV, avec invalidation par mtime + taille."""
    rel_path = str(fits_file.relative_to(REDUCTIONS_PATH)).replace('\\', '/')
    try:
        st = fits_file.stat()
    except Exception:
        return None, False

    cached = cache.get(rel_path)
    if cached is not None:
        cm = cached.get('mtime', np.nan)
        cs = cached.get('size', -1)
        # Invalidation simple et robuste pour gros volumes d'archives.
        if pd.notna(cm) and abs(float(cm) - float(st.st_mtime)) < 1e-6 and int(cs) == int(st.st_size):
            return cached.get('header'), False

    header = read_fits_headers(fits_file)
    if header is None:
        return None, False

    cache[rel_path] = {
        'mtime': float(st.st_mtime),
        'size': int(st.st_size),
        'header': header,
    }
    return header, True


def generate_fits_preview_png(fits_file, png_file):
    """Genere un PNG avec image raster et overlays grille/echelle/boussole."""
    try:
        with fits.open(fits_file) as hdul:
            header = hdul[0].header
            data = hdul[0].data
            if data is None:
                return False
            while hasattr(data, 'ndim') and data.ndim > 2:
                data = data[0]
            arr = np.array(data, dtype=float)
            finite = np.isfinite(arr)
            if not np.any(finite):
                return False
            vmin, vmax = np.percentile(arr[finite], [1, 99])
            if vmax <= vmin:
                vmax = vmin + 1.0
            arr = np.clip(arr, vmin, vmax)
            arr = np.where(np.isfinite(arr), arr, vmin)
            arr = (arr - vmin) / (vmax - vmin)
            arr8 = np.uint8(np.clip(arr * 255.0, 0, 255))
            png_file.parent.mkdir(parents=True, exist_ok=True)
            wcs = None
            try:
                wcs = WCS(header)
                if not wcs.has_celestial:
                    wcs = None
            except Exception:
                wcs = None
            fig = plt.figure(figsize=(8, 8), dpi=200)
            if wcs is not None:
                ax = fig.add_subplot(111, projection=wcs.celestial)
                ax.imshow(arr8, origin='lower', cmap='gray', interpolation='nearest')
                # Grille RA/DEC propre
                ax.coords.grid(True, color='#66d9ff', ls=':', lw=0.7, alpha=0.9)
                ax.coords[0].set_axislabel('RA')
                ax.coords[1].set_axislabel('Dec')
                ax.coords[0].set_major_formatter('hh:mm')
                ax.coords[1].set_major_formatter('dd:mm')
                # Espacement auto pour éviter les répétitions
                try:
                    ax.coords[0].set_ticks(spacing=6 * u.arcmin)
                    ax.coords[1].set_ticks(spacing=6 * u.arcmin)
                except Exception:
                    pass
                try:
                    ax.coords[0].set_ticks_position('b')
                    ax.coords[0].set_ticklabel_position('b')
                    ax.coords[1].set_ticks_position('l')
                    ax.coords[1].set_ticklabel_position('l')
                except Exception:
                    pass
                ax.tick_params(labelsize=8)
                ny, nx = arr8.shape
                # Echelle angulaire
                try:
                    pix_scales_deg = proj_plane_pixel_scales(wcs.celestial)
                    arcsec_per_pix = float(np.mean(pix_scales_deg) * 3600.0)
                    scale_arcsec = 60
                    scale_px = scale_arcsec / arcsec_per_pix
                    x0 = 0.08 * nx
                    y0 = 0.07 * ny
                    x1 = x0 + scale_px
                    ax.plot([x0, x1], [y0, y0], color='white', lw=2.2)
                    ax.plot([x0, x0], [y0 - 0.01 * ny, y0 + 0.01 * ny], color='white', lw=1.6)
                    ax.plot([x1, x1], [y0 - 0.01 * ny, y0 + 0.01 * ny], color='white', lw=1.6)
                    label = f"{int(scale_arcsec / 60)} arcmin"
                    ax.text((x0 + x1) / 2, y0 + 0.025 * ny, label, color='white', fontsize=8, ha='center', va='bottom')
                except Exception:
                    pass
                # Boussole N/E
                try:
                    cx = 0.90 * nx
                    cy = 0.11 * ny
                    base = wcs.celestial.pixel_to_world(cx, cy)
                    sep = 2.5 * u.arcmin
                    north = base.directional_offset_by(0 * u.deg, sep)
                    east = base.directional_offset_by(90 * u.deg, sep)
                    nxp, nyp = wcs.celestial.world_to_pixel(north)
                    exp, eyp = wcs.celestial.world_to_pixel(east)
                    ax.annotate('', xy=(nxp, nyp), xytext=(cx, cy), arrowprops=dict(arrowstyle='-|>', color='white', lw=1.8))
                    ax.annotate('', xy=(exp, eyp), xytext=(cx, cy), arrowprops=dict(arrowstyle='-|>', color='white', lw=1.8))
                    ax.text(nxp, nyp, 'N', color='white', fontsize=8, ha='center', va='bottom')
                    ax.text(exp, eyp, 'E', color='white', fontsize=8, ha='left', va='center')
                except Exception:
                    pass
            else:
                ax = fig.add_subplot(111)
                ax.imshow(arr8, origin='lower', cmap='gray', interpolation='nearest')
                ax.axis('off')
            fig.tight_layout(pad=0.2)
            fig.savefig(png_file, format='png', dpi=200)
            plt.close(fig)
            plt.close('all')  # Force close all figures
            gc.collect()  # Force garbage collection
            return True
    except Exception:
        plt.close('all')
        gc.collect()
        return False


def collect_observation_data(demo=False):
    """Parcourt reductions/ et collecte les observations FITS."""
    observations = []
    phot_stats = []
    # Evite de relire les memes entetes a chaque execution.
    header_cache = load_header_cache(HEADER_CACHE_PATH)
    cache_dirty = False

    night_dirs = sorted(REDUCTIONS_PATH.glob('*/'))
    
    # Mode demo: selectionne 20 nuits uniformément reparties
    if demo and len(night_dirs) > 20:
        indices = np.linspace(0, len(night_dirs) - 1, 20, dtype=int)
        night_dirs = [night_dirs[i] for i in indices]
        emoji_print(f"\n📂 MODE DEMO: {len(night_dirs)} nuits uniformément distribuées\n")
    else:
        emoji_print(f"\n📂 Scan des nuits d'observation: {REDUCTIONS_PATH}\n")
    for night_dir in tqdm(night_dirs, desc="Nuits", unit="night"):
        if not night_dir.is_dir():
            continue

        nightid = night_dir.name
        night_dt, year_label, night_label = parse_nightid(nightid)

        fits_files = sorted(night_dir.glob('*_[JHI].fits')) + sorted(night_dir.glob('*_HeI.fits'))
        for fits_file in tqdm(fits_files, desc=f"  {nightid}", unit="FITS", leave=False):
            if '_PSF' in fits_file.name:
                continue

            header, was_updated = read_fits_headers_cached(fits_file, header_cache)
            if header is None:
                continue
            cache_dirty = cache_dirty or was_updated

            parts = fits_file.stem.split('_')
            target = '_'.join(parts[1:-1]) if len(parts) > 2 else 'Unknown'
            filter_type = parts[-1]

            png_abs = ASSETS_PATH / 'png_previews' / nightid / f'{fits_file.stem}.png'
            if REGENERATE_PREVIEWS or (not png_abs.exists()) or (png_abs.stat().st_mtime < fits_file.stat().st_mtime):
                generate_fits_preview_png(fits_file, png_abs)

            observations.append({
                'nightid': nightid,
                'night_label': night_label,
                'year': year_label,
                'filename': fits_file.name,
                'target': target,
                'filter': filter_type,
                'date_obs': header['date_obs'],
                'exptime': header['exptime'],
                'fwhm': header['fwhm'],
                'zp1s': header['zp1s'],
                'ra': header['ra'],
                'dec': header['dec'],
                'shape': header['shape'],
                'path': str(fits_file.relative_to(REDUCTIONS_PATH)),
                'png_preview': to_web_path(png_abs),
            })

        # Les phot_ enrichissent les graphes de qualite, mais restent hors tableau principal.
        phot_files = sorted({
            *night_dir.glob('phot*.fits'),
            *night_dir.glob('phot*.fits.gz'),
            *night_dir.glob('*phot*.fits'),
            *night_dir.glob('*phot*.fits.gz'),
        })
        for phot_file in tqdm(phot_files, desc=f"  {nightid} (phot)", unit="phot", leave=False):
            if '_PSF' in phot_file.name:
                continue

            header, was_updated = read_fits_headers_cached(phot_file, header_cache)
            if header is None:
                continue
            cache_dirty = cache_dirty or was_updated

            filter_type = header.get('filter', 'N/A')
            target = header.get('object', phot_file.stem)

            phot_stats.append({
                'nightid': nightid,
                'night_label': night_label,
                'year': year_label,
                'filename': phot_file.name,
                'target': target,
                'filter': filter_type,
                'date_obs': header.get('date_obs', ''),
                'exptime': header.get('exptime', np.nan),
                'fwhm': header.get('fwhm', np.nan),
                'zp1s': header.get('zp1s', np.nan),
                'ra': header.get('ra', np.nan),
                'dec': header.get('dec', np.nan),
                'shape': header.get('shape', (0, 0)),
                'path': str(phot_file.relative_to(REDUCTIONS_PATH)),
            })

    psf_previews = {}
    night_dirs_psf = sorted(REDUCTIONS_PATH.glob('*/'))
    emoji_print("\n🌟 Chargement des cartes PSF\n")
    for night_dir in tqdm(night_dirs_psf, desc="Nuits PSF", unit="night"):
        if not night_dir.is_dir():
            continue

        psf_png_files = sorted(night_dir.glob('*_PSF.png')) + sorted(night_dir.glob('*_PSF.PNG'))
        for psf_png in psf_png_files:
            psf_previews[psf_png.stem] = to_web_path(psf_png)

    # Re-ecriture du cache uniquement si au moins une entree a ete actualisee.
    if cache_dirty:
        save_header_cache(HEADER_CACHE_PATH, header_cache)

    return observations, phot_stats, psf_previews


def build_bokeh_night_plots(observations, phot_stats=None):
    """Construit les graphiques Bokeh interactifs par nuit."""
    obs_df = pd.DataFrame(observations)
    phot_df = pd.DataFrame(phot_stats or [])

    if not obs_df.empty:
        obs_df = obs_df.copy()
        obs_df['source_type'] = 'reduit'
    if not phot_df.empty:
        phot_df = phot_df.copy()
        phot_df['source_type'] = 'phot'

    df = pd.concat([x for x in [obs_df, phot_df] if not x.empty], ignore_index=True)
    if df.empty:
        return '', {}

    df['date_dt'] = pd.to_datetime(df['date_obs'], errors='coerce')

    plot_map = {}
    nightids = sorted(df['nightid'].unique(), reverse=True)
    for nightid in tqdm(nightids, desc="Plots Bokeh", unit="night"):
        nd = df[df['nightid'] == nightid].copy()
        nd = nd.dropna(subset=['date_dt']).sort_values('date_dt')

        p1 = figure(
            title=f'Nuit {nightid} - FWHM',
            x_axis_type='datetime',
            height=250,
            sizing_mode='stretch_width',
            tools='pan,wheel_zoom,box_zoom,reset,save',
            toolbar_location='right',
        )
        p1.yaxis.axis_label = 'FWHM (pixels)'

        p2 = figure(
            title=f'Nuit {nightid} - ZP1S',
            x_axis_type='datetime',
            height=250,
            sizing_mode='stretch_width',
            tools='pan,wheel_zoom,box_zoom,reset,save',
            toolbar_location='right',
        )
        p2.yaxis.axis_label = 'ZP1S (mag)'

        for band in sorted(nd['filter'].dropna().unique(), key=filter_sort_key):
            bd = nd[nd['filter'] == band]
            color = filter_color(band)

            bd_fwhm = bd.dropna(subset=['fwhm'])
            if not bd_fwhm.empty:
                src1 = ColumnDataSource(bd_fwhm)
                r1 = p1.scatter(
                    x='date_dt', y='fwhm', source=src1, size=8,
                    color=color, alpha=0.9, legend_label=band,
                )
                hover1 = HoverTool(
                    renderers=[r1],
                    tooltips=[
                        ('Objet', '@target'),
                        ('Bande', '@filter'),
                        ('Type', '@source_type'),
                        ('Date', '@date_obs'),
                        ('FWHM', '@fwhm{0.000}'),
                    ],
                )
                p1.add_tools(hover1)

            bd_zp = bd.dropna(subset=['zp1s'])
            if not bd_zp.empty:
                src2 = ColumnDataSource(bd_zp)
                r2 = p2.scatter(
                    x='date_dt', y='zp1s', source=src2, size=8,
                    color=color, alpha=0.9, legend_label=band,
                )
                hover2 = HoverTool(
                    renderers=[r2],
                    tooltips=[
                        ('Objet', '@target'),
                        ('Bande', '@filter'),
                        ('Type', '@source_type'),
                        ('Date', '@date_obs'),
                        ('ZP1S', '@zp1s{0.00}'),
                    ],
                )
                p2.add_tools(hover2)

        p1.legend.click_policy = 'hide'
        p2.legend.click_policy = 'hide'
        p1.grid.grid_line_alpha = 0.25
        p2.grid.grid_line_alpha = 0.25

        plot_map[nightid] = row(p1, p2, sizing_mode='stretch_width')

    if not plot_map:
        return '', {}

    script, divs = components(plot_map)
    return script, divs


def create_search_section():
    return '''
    <div class="search-section">
        <h2>Recherche</h2>
        <div class="search-controls">
            <div class="search-group">
                <label for="search-object">Nom de l'objet:</label>
                <input type="text" id="search-object" placeholder="ex.: J1426+50" oninput="filterObservations()">
            </div>
            <div class="search-group">
                <label for="search-ra">RA (deg):</label>
                <input type="number" id="search-ra" placeholder="ex.: 216.68" step="0.01" oninput="filterObservations()">
            </div>
            <div class="search-group">
                <label for="search-dec">Dec (deg):</label>
                <input type="number" id="search-dec" placeholder="ex.: 50.11" step="0.01" oninput="filterObservations()">
            </div>
            <div class="search-group">
                <label for="search-radius">Rayon (deg):</label>
                <input type="number" id="search-radius" value="1.0" step="0.1" min="0.01" oninput="filterObservations()">
            </div>
            <button onclick="clearSearch()" class="clear-btn">Effacer</button>
        </div>
        <div id="search-results" class="search-results" style="display: none;"></div>
    </div>
'''


def create_year_date_sections(observations, bokeh_divs, psf_previews):
    """Construit la hierarchie Annee -> Date."""
    df = pd.DataFrame(observations)
    if df.empty:
        return '<p>Aucune observation trouvee.</p>'

    df = df.sort_values(['year', 'nightid', 'date_obs', 'target'], ascending=[False, False, False, True])

    html = ['<div class="year-list">']

    years = sorted(df['year'].dropna().unique(), reverse=True)
    for year in tqdm(years, desc="Structure HTML", unit="year"):
        ydf = df[df['year'] == year]
        n_nights = ydf['nightid'].nunique()
        n_obs = len(ydf)

        html.append(
            f'<details class="year-item" data-year="{year}">'
            f'<summary><span class="year-title">Annee {year}</span>'
            f'<span class="year-meta">{n_nights} nuits | {n_obs} observations</span></summary>'
            '<div class="year-content">'
        )

        for nightid in sorted(ydf['nightid'].unique(), reverse=True):
            ndf = ydf[ydf['nightid'] == nightid]
            night_label = ndf['night_label'].iloc[0]
            n_targets = ndf['target'].nunique()
            sorted_filters = sorted(ndf['filter'].dropna().unique(), key=filter_sort_key)
            bands = ', '.join(format_filter_html(f) for f in sorted_filters)
            total_exptime = ndf['exptime'].sum()

            bokeh_fallback = '<div class="plot-fallback">Graphique indisponible</div>'
            bokeh_content = bokeh_divs.get(nightid, bokeh_fallback)
            html.append(
                f'<details class="night-item" data-nightid="{nightid}" data-year="{year}">'
                f'<summary><span class="night-title">Date {night_label}</span>'
                f'<span class="night-meta">{len(ndf)} obs | {n_targets} objets | {bands}</span></summary>'
                '<div class="night-content">'
                '<div class="night-stats">'
                f'<div class="stat-item"><span class="stat-name">Observations:</span><span class="stat-value">{len(ndf)}</span></div>'
                f'<div class="stat-item"><span class="stat-name">Objets uniques:</span><span class="stat-value">{n_targets}</span></div>'
                f'<div class="stat-item"><span class="stat-name">Bandes:</span><span class="stat-value">{bands}</span></div>'
                f'<div class="stat-item"><span class="stat-name">Exposition totale:</span><span class="stat-value">{total_exptime:.1f} s</span></div>'
                '</div>'
                '<div class="night-plot-wrap">'
                + bokeh_content +
                '</div>'
                '<div class="table-scroll">'
                f'<table class="data-table night-table" data-nightid="{nightid}">'
                '<thead><tr>'
                '<th>Objet</th><th>Bande</th><th>Date-Obs</th><th>Exp. (s)</th><th>FWHM</th><th>ZP1S</th><th>RA (deg)</th><th>Dec (deg)</th><th>Apercu</th><th>Carte PSF</th><th>Fichier</th>'
                '</tr></thead><tbody>'
            )

            for _, row in ndf.iterrows():
                fwhm_str = f"{row['fwhm']:.3f}" if pd.notna(row['fwhm']) else 'N/A'
                zp1s_str = f"{row['zp1s']:.2f}" if pd.notna(row['zp1s']) else 'N/A'
                png_src = row.get('png_preview', '')
                if png_src:
                    # Build button HTML without f-string backslashes (Python < 3.12 compat)
                    js_call = "openImageModal('" + png_src.replace("'", "\\'") + "', true)"
                    preview_btn = '<button type="button" class="preview-btn" data-img="{}" onclick="{}" title="Ouvrir l\'image astronomique avec loupe">🔭</button>'.format(png_src, js_call)
                    preview_cell = '<td class="preview-cell">' + preview_btn + '</td>'
                else:
                    preview_cell = '<td>-</td>'

                psf_key = Path(str(row['filename'])).stem.replace('.fits', '')
                psf_key = f"{psf_key}_PSF"
                psf_src = psf_previews.get(psf_key, '')
                if psf_src:
                    # Build button HTML without f-string backslashes (Python < 3.12 compat)
                    js_call_psf = "openImageModal('" + psf_src.replace("'", "\\'") + "', false)"
                    psf_btn = '<button type="button" class="preview-btn psf-btn" onclick="{}" title="Ouvrir la carte PSF">⭐</button>'.format(js_call_psf)
                    psf_cell = '<td class="preview-cell psf-cell">' + psf_btn + '</td>'
                else:
                    psf_cell = '<td>-</td>'

                html.append(
                    '<tr>'
                    f'<td>{row["target"]}</td>'
                    f'<td>{format_filter_html(row["filter"])}</td>'
                    f'<td>{str(row["date_obs"])[:19]}</td>'
                    f'<td>{row["exptime"]:.1f}</td>'
                    f'<td>{fwhm_str}</td>'
                    f'<td>{zp1s_str}</td>'
                    f'<td>{row["ra"]:.4f}</td>'
                    f'<td>{row["dec"]:.4f}</td>'
                    f'{preview_cell}'
                    f'{psf_cell}'
                    f'<td class="filename">{row["filename"]}</td>'
                    '</tr>'
                )

            html.append('</tbody></table></div></div></details>')

        html.append('</div></details>')

    html.append('</div>')
    return ''.join(html)


def generate_html(observations, psf_previews, phot_stats=None):
    """Construit le HTML final."""
    bokeh_script, bokeh_divs = build_bokeh_night_plots(observations, phot_stats=phot_stats)
    print("   Mise en page du tableau de bord...")
    sections_html = create_year_date_sections(observations, bokeh_divs, psf_previews)
    search_html = create_search_section()
    bokeh_resources = INLINE.render()

    html = f"""<!DOCTYPE html>
<html lang="fr">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Tableau de bord OMM CPAPIR</title>
    {bokeh_resources}
    <style>
        * {{ box-sizing: border-box; margin: 0; padding: 0; }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            min-height: 100vh;
            padding: 20px;
        }}
        .container {{
            max-width: 1440px;
            margin: 0 auto;
            background: white;
            border-radius: 12px;
            box-shadow: 0 10px 40px rgba(0,0,0,0.3);
            overflow: hidden;
        }}
        header {{
            background: linear-gradient(135deg, #2c3e50 0%, #3498db 100%);
            color: white;
            padding: 36px 20px;
            text-align: center;
        }}
        header h1 {{ font-size: 2.2em; margin-bottom: 8px; }}
        header p {{ font-size: 1.05em; opacity: 0.95; }}
        .content {{ padding: 30px; }}
        .section {{ margin-bottom: 28px; }}
        .section h2 {{
            font-size: 1.7em;
            color: #2c3e50;
            margin-bottom: 16px;
            padding-bottom: 8px;
            border-bottom: 3px solid #667eea;
        }}

        .search-section {{
            background: #f8f9fa;
            padding: 22px;
            border-radius: 8px;
            box-shadow: 0 2px 10px rgba(0,0,0,0.05);
        }}
        .search-controls {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
            gap: 14px;
            margin-bottom: 12px;
        }}
        .search-group {{ display: flex; flex-direction: column; }}
        .search-group label {{ font-weight: 600; margin-bottom: 6px; color: #333; font-size: 0.92em; }}
        .search-group input {{
            padding: 9px;
            border: 2px solid #ddd;
            border-radius: 4px;
            font-size: 0.95em;
        }}
        .clear-btn {{
            padding: 9px 14px;
            background: #667eea;
            color: white;
            border: none;
            border-radius: 4px;
            font-size: 0.95em;
            font-weight: 600;
            cursor: pointer;
            align-self: end;
        }}
        .search-results {{
            background: white;
            padding: 12px;
            border: 2px solid #667eea;
            border-radius: 4px;
            margin-top: 10px;
        }}

        .actions {{ display: flex; justify-content: flex-end; gap: 8px; margin-bottom: 10px; }}
        .toggle-all-btn {{
            padding: 8px 14px;
            border: 1px solid #c9d2e6;
            background: #f4f7ff;
            color: #27314f;
            border-radius: 6px;
            font-size: 0.9em;
            font-weight: 600;
            cursor: pointer;
        }}

        .year-list {{ display: flex; flex-direction: column; gap: 10px; }}
        .year-item, .night-item {{
            border: 1px solid #dfe3ea;
            border-radius: 8px;
            background: white;
            box-shadow: 0 1px 4px rgba(0,0,0,0.04);
        }}
        .year-item > summary, .night-item > summary {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 10px 12px;
            cursor: pointer;
            list-style: none;
            user-select: none;
        }}
        .year-item > summary {{ background: #eef2fb; color: #1f2a44; }}
        .night-item > summary {{ background: #f7f9fd; color: #1f2a44; }}
        .year-item > summary::-webkit-details-marker,
        .night-item > summary::-webkit-details-marker {{ display: none; }}

        .year-title {{ font-size: 1em; font-weight: 700; }}
        .year-meta, .night-meta {{ font-size: 0.9em; color: #4c5875; }}
        .night-title {{ font-size: 0.98em; font-weight: 600; }}

        .year-content {{ padding: 8px; display: flex; flex-direction: column; gap: 8px; }}
        .night-content {{ padding: 0 10px 10px 10px; }}

        .night-stats {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(170px, 1fr));
            gap: 8px;
            padding: 10px 0;
            border-bottom: 1px solid #eee;
            margin-bottom: 10px;
        }}
        .stat-item {{
            display: flex;
            justify-content: space-between;
            padding: 8px;
            background: #f8f9fa;
            border-radius: 4px;
        }}
        .stat-name {{ font-weight: 600; color: #333; }}
        .stat-value {{ color: #667eea; font-weight: 600; }}

        .night-plot-wrap {{
            margin: 8px 0 12px 0;
            padding: 8px;
            border: 1px solid #e7ebf3;
            border-radius: 8px;
            background: #fbfcff;
        }}

        .table-scroll {{ width: 100%; overflow-x: auto; }}
        .data-table {{ width: 100%; border-collapse: collapse; font-size: 0.82em; }}
        .data-table thead {{ background: #667eea; color: white; }}
        .data-table th {{ padding: 10px; text-align: left; font-weight: 600; }}
        .data-table td {{ padding: 8px 10px; border-bottom: 1px solid #e0e0e0; }}
        .data-table tbody tr:nth-child(even) {{ background-color: #f8f9fa; }}

        .filename {{ font-family: 'Courier New', monospace; font-size: 0.84em; color: #555; }}

        .preview-cell {{ position: relative; min-width: 70px; text-align: center; }}
        .psf-cell {{ min-width: 90px; }}
        .preview-btn {{
            border: 1px solid #d0d7eb;
            background: #fff;
            border-radius: 999px;
            width: 30px;
            height: 30px;
            cursor: pointer;
            font-size: 16px;
            line-height: 1;
        }}
        .psf-btn {{
            width: 36px;
            height: 36px;
            font-size: 18px;
            border-color: #95a7d8;
            background: #f7f9ff;
        }}

        .image-modal {{
            display: none;
            position: fixed;
            z-index: 2000;
            inset: 0;
            background: rgba(8, 12, 24, 0.82);
            align-items: center;
            justify-content: center;
            padding: 20px;
        }}
        .image-modal.show {{ display: flex; }}

        .image-modal-content {{
            width: min(96vw, 1300px);
            max-height: 94vh;
            background: #ffffff;
            border-radius: 12px;
            box-shadow: 0 20px 40px rgba(0,0,0,0.35);
            overflow: hidden;
            display: flex;
            flex-direction: column;
        }}

        .image-modal-header {{
            display: flex;
            align-items: center;
            justify-content: space-between;
            padding: 10px 14px;
            border-bottom: 1px solid #e5e9f2;
            background: #f8faff;
        }}

        .image-modal-title {{ font-size: 0.95em; color: #2a3552; font-weight: 600; }}

        .modal-close {{
            border: none;
            background: #e9eefb;
            color: #1f2a44;
            border-radius: 6px;
            width: 32px;
            height: 32px;
            cursor: pointer;
            font-size: 20px;
            line-height: 1;
        }}

        .modal-toolbar {{
            display: flex;
            align-items: center;
            gap: 10px;
            padding: 8px 14px;
            border-bottom: 1px solid #edf1f9;
            background: #fcfdff;
            color: #33415f;
            font-size: 0.9em;
        }}

        .modal-toolbar input[type="range"] {{ width: 200px; }}

        .modal-image-wrap {{
            position: relative;
            overflow: auto;
            background: #0f1627;
            padding: 14px;
            text-align: center;
            cursor: crosshair;
        }}

        .modal-image-wrap img {{
            max-width: 100%;
            max-height: calc(94vh - 160px);
            width: auto;
            height: auto;
            display: inline-block;
            border-radius: 6px;
            box-shadow: 0 10px 24px rgba(0,0,0,0.35);
            image-rendering: pixelated;
            image-rendering: crisp-edges;
        }}

        .magnifier-lens {{
            position: absolute;
            display: none;
            width: 220px;
            height: 220px;
            border: 2px solid #f8fbff;
            border-radius: 50%;
            pointer-events: none;
            box-shadow: 0 0 0 2px rgba(15, 22, 39, 0.5), 0 10px 20px rgba(0,0,0,0.4);
            background: #000;
            z-index: 3;
        }}

        footer {{
            background: #f8f9fa;
            padding: 16px;
            text-align: center;
            color: #666;
            border-top: 1px solid #e0e0e0;
            font-size: 0.9em;
        }}

        @media (max-width: 768px) {{
            .content {{ padding: 16px; }}
            .search-controls {{ grid-template-columns: 1fr; }}
            .data-table {{ font-size: 0.74em; }}
            .psf-cell {{ min-width: 80px; }}
        }}
    </style>
</head>
<body>
    <div class="container">
        <header>
            <h1>OMM CPAPIR</h1>
            <p>Tableau de bord des donnees reduites</p>
        </header>

        <div class="content">
            <section class="section">{search_html}</section>

            <section class="section">
                <h2>Observations par annee et date</h2>
                <div class="actions">
                    <button id="toggle-all-years" class="toggle-all-btn" onclick="toggleAllYears()">Tout ouvrir</button>
                    <button id="toggle-all-dates" class="toggle-all-btn" onclick="toggleAllDates()">Ouvrir les dates</button>
                </div>
                {sections_html}
            </section>
        </div>

        <footer>
            <p>Page generee: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</p>
            <p>Source des donnees: {REDUCTIONS_PATH}</p>
        </footer>
    </div>

    <div id="image-modal" class="image-modal" onclick="closeImageModal(event)">
        <div class="image-modal-content" onclick="event.stopPropagation()">
            <div class="image-modal-header">
                <div class="image-modal-title" id="image-modal-title">Apercu image</div>
                <button type="button" class="modal-close" onclick="closeImageModal()" title="Fermer">&times;</button>
            </div>
            <div class="modal-toolbar">
                <span>Loupe:</span>
                <input id="magnifier-zoom" type="range" min="4" max="24" step="1" value="10" oninput="setMagnifierZoom(this.value)">
                <span id="magnifier-zoom-label">x10</span>
            </div>
            <div id="modal-image-wrap" class="modal-image-wrap">
                <img id="modal-image" alt="Image FITS complete">
                <canvas id="magnifier-lens" class="magnifier-lens" width="220" height="220"></canvas>
            </div>
        </div>
    </div>

    {bokeh_script}

    <script>
        const allObservations = {json.dumps(observations)};
        let yearsOpen = false;
        let datesOpen = false;
        let magnifierZoom = 10;
        let modalMagnifierEnabled = true;

        function setMagnifierZoom(value) {{
            magnifierZoom = Number(value || 6);
            document.getElementById('magnifier-zoom-label').textContent = `x${{magnifierZoom}}`;
        }}

        function openImageModal(src, useMagnifier = true) {{
            if (!src) return;
            const modal = document.getElementById('image-modal');
            const img = document.getElementById('modal-image');
            const title = document.getElementById('image-modal-title');
            const lens = document.getElementById('magnifier-lens');
            const toolbar = document.querySelector('.modal-toolbar');
            img.setAttribute('src', src);
            title.textContent = src.split('/').pop() || 'Apercu image';
            lens.style.display = 'none';
            modalMagnifierEnabled = !!useMagnifier;
            toolbar.style.display = modalMagnifierEnabled ? 'flex' : 'none';
            modal.classList.add('show');
        }}

        function closeImageModal(event) {{
            if (event && event.target && event.target.closest('.image-modal-content')) return;
            const modal = document.getElementById('image-modal');
            const lens = document.getElementById('magnifier-lens');
            modal.classList.remove('show');
            lens.style.display = 'none';
        }}

        function updateMagnifier(event) {{
            if (!modalMagnifierEnabled) return;
            const img = document.getElementById('modal-image');
            const lens = document.getElementById('magnifier-lens');
            if (!img.complete || !img.naturalWidth || !img.naturalHeight) return;

            const ctx = lens.getContext('2d');
            if (!ctx) return;
            ctx.imageSmoothingEnabled = false;

            const rect = img.getBoundingClientRect();
            const x = event.clientX - rect.left;
            const y = event.clientY - rect.top;
            const inside = x >= 0 && y >= 0 && x <= rect.width && y <= rect.height;

            if (!inside) {{
                lens.style.display = 'none';
                return;
            }}

            const lensW = lens.offsetWidth || 220;
            const lensH = lens.offsetHeight || 220;
            lens.style.display = 'block';
            lens.style.left = `${{img.offsetLeft + x - lensW / 2}}px`;
            lens.style.top = `${{img.offsetTop + y - lensH / 2}}px`;

            const scaleX = img.naturalWidth / rect.width;
            const scaleY = img.naturalHeight / rect.height;
            const px = x * scaleX;
            const py = y * scaleY;

            const srcW = lens.width / magnifierZoom;
            const srcH = lens.height / magnifierZoom;

            let sx = px - srcW / 2;
            let sy = py - srcH / 2;
            sx = Math.max(0, Math.min(img.naturalWidth - srcW, sx));
            sy = Math.max(0, Math.min(img.naturalHeight - srcH, sy));

            ctx.clearRect(0, 0, lens.width, lens.height);
            ctx.drawImage(img, sx, sy, srcW, srcH, 0, 0, lens.width, lens.height);
        }}

        function toggleAllYears() {{
            yearsOpen = !yearsOpen;
            document.querySelectorAll('.year-item').forEach(item => {{
                if (item.style.display !== 'none') item.open = yearsOpen;
            }});
            document.getElementById('toggle-all-years').textContent = yearsOpen ? 'Tout fermer' : 'Tout ouvrir';
        }}

        function toggleAllDates() {{
            datesOpen = !datesOpen;
            document.querySelectorAll('.year-item').forEach(yearItem => {{
                if (yearItem.style.display !== 'none' && datesOpen) yearItem.open = true;
                yearItem.querySelectorAll('.night-item').forEach(nightItem => {{
                    if (nightItem.style.display !== 'none') nightItem.open = datesOpen;
                }});
            }});
            document.getElementById('toggle-all-dates').textContent = datesOpen ? 'Fermer les dates' : 'Ouvrir les dates';
        }}

        function applyYearVisibilityFromNights() {{
            document.querySelectorAll('.year-item').forEach(yearItem => {{
                const visibleNights = Array.from(yearItem.querySelectorAll('.night-item')).some(n => n.style.display !== 'none');
                yearItem.style.display = visibleNights ? 'block' : 'none';
            }});
        }}

        function filterObservations() {{
            const objectSearch = document.getElementById('search-object').value.toLowerCase();
            const raSearch = document.getElementById('search-ra').value;
            const decSearch = document.getElementById('search-dec').value;
            const radius = parseFloat(document.getElementById('search-radius').value) || 1.0;

            let filtered = allObservations;

            if (objectSearch) {{
                filtered = filtered.filter(obs => obs.target.toLowerCase().includes(objectSearch));
            }}

            if (raSearch && decSearch) {{
                const targetRa = parseFloat(raSearch);
                const targetDec = parseFloat(decSearch);
                filtered = filtered.filter(obs => {{
                    if (obs.ra === 0 || obs.dec === 0) return false;
                    const dRa = obs.ra - targetRa;
                    const dDec = obs.dec - targetDec;
                    const distance = Math.sqrt(dRa * dRa + dDec * dDec);
                    return distance <= radius;
                }});
            }}

            const hasFilter = objectSearch || (raSearch && decSearch);
            const resultsDiv = document.getElementById('search-results');

            if (hasFilter) {{
                resultsDiv.style.display = 'block';
                resultsDiv.innerHTML = `<strong>${{filtered.length}} observation(s) trouvee(s)</strong>`;
                const matchingNights = new Set(filtered.map(obs => obs.nightid));
                document.querySelectorAll('.night-item').forEach(item => {{
                    item.style.display = matchingNights.has(item.dataset.nightid) ? 'block' : 'none';
                }});
                applyYearVisibilityFromNights();
            }} else {{
                resultsDiv.style.display = 'none';
                document.querySelectorAll('.night-item').forEach(item => item.style.display = 'block');
                document.querySelectorAll('.year-item').forEach(item => item.style.display = 'block');
            }}
        }}

        function clearSearch() {{
            document.getElementById('search-object').value = '';
            document.getElementById('search-ra').value = '';
            document.getElementById('search-dec').value = '';
            document.getElementById('search-results').style.display = 'none';
            document.querySelectorAll('.night-item').forEach(item => item.style.display = 'block');
            document.querySelectorAll('.year-item').forEach(item => item.style.display = 'block');
        }}

        document.addEventListener('keydown', (event) => {{
            if (event.key === 'Escape') closeImageModal();
        }});

        document.getElementById('modal-image-wrap').addEventListener('mousemove', updateMagnifier);
        document.getElementById('modal-image-wrap').addEventListener('mouseleave', () => {{
            document.getElementById('magnifier-lens').style.display = 'none';
        }});
    </script>
</body>
</html>
"""
    return html


def main(no_sync=False, demo=False):
    emoji_print(f'\n📡 Scanning observations in {REDUCTIONS_PATH}...')
    observations, phot_stats, psf_previews = collect_observation_data(demo=demo)
    emoji_print(f'\n✅ Found {len(observations)} observations + {len(phot_stats)} phot files')

    emoji_print('\n📊 Construction des graphiques Bokeh interactifs...')
    html = generate_html(observations, psf_previews, phot_stats=phot_stats)
    with open(OUTPUT_PATH, 'w', encoding='utf-8') as f:
        f.write(html)

    emoji_print(f'\n✨ OK Tableau de bord genere: {OUTPUT_PATH}')
    print(f'   Open in browser: file://{OUTPUT_PATH}')
    sync_web_archive(no_sync=no_sync)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Generation du tableau de bord CPAPIR')
    parser.add_argument(
        '--no-sync',
        action='store_true',
        help='Desactive la synchronisation rsync web pour cette execution.'
    )
    parser.add_argument(
        '--demo',
        action='store_true',
        help='Mode demo: utilise 20 nuits uniformément reparties pour un test rapide.'
    )
    args = parser.parse_args()
    main(no_sync=args.no_sync, demo=args.demo)
