# OMM CPAPIR Data Dashboard

Tableau de bord interactif pour l'archive CPAPIR (OMM/CFHT): navigation par nuits, graphes de qualite, apercus scientifiques et cartes PSF.

## Fonctionnalites

- Exploration hierarchique annee -> nuit
- Graphes interactifs Bokeh (FWHM et ZP1S)
- Inclusion des fichiers `phot_*.fits.gz` dans les graphes (sans polluer la table principale)
- Apercu image scientifique en PNG avec grille RA/Dec, echelle, boussole, et loupe
- Apercu PSF via popup dedie (sans loupe)
- Couleurs des points triees par longueur d'onde des filtres
- Cache CSV des entetes FITS pour accelerer les runs volumineux
- Configuration portable par usager via YAML
- Synchronisation web automatique (rsync) sur serveur `cpapir`

## Structure

```text
log_cpapir/
├── reductions/                    # Donnees reduites (non versionnees)
├── dashboard_assets/              # Previews et cache (non versionnes)
│   ├── png_previews/
│   └── header_cache.csv
├── generate_dashboard.py
├── dashboard_config.yaml          # Versionne (base commune)
├── dashboard_config.local.yaml    # Optionnel, local, ignore par git
├── dashboard.html                 # Sortie
├── .gitignore
└── README.md
```

## Installation locale (Mac/Linux)

1. Cloner le depot et entrer dans le dossier.
2. Creer un environnement conda Python 3.12.
3. Installer les dependances Python.

Exemple:

```bash
conda create -n cpapir_dashboard python=3.12 -y
conda activate cpapir_dashboard
pip install numpy pandas astropy matplotlib bokeh pyyaml pillow
```

Execution:

```bash
python generate_dashboard.py
```

## Installation sur la machine CPAPIR

Objectif: apres un `git pull`, la commande doit fonctionner directement avec les bons chemins et la sync web.

### 1) Prerequis

- Usager: `cpapir`
- Dossier reductions: `/data/cpapir/reductions`
- Dossier assets: `/data/cpapir/dashboard_assets`
- Acces SSH vers la cible web configuree

### 2) Recuperer le code

```bash
cd /data/cpapir
git clone https://github.com/eartigau/log_cpapir.git log_cpapir
cd log_cpapir
git pull
```

### 3) Environnement Python

```bash
conda create -n cpapir_dashboard python=3.12 -y
conda activate cpapir_dashboard
pip install numpy pandas astropy matplotlib bokeh pyyaml pillow
```

### 4) Verifier la config

Le fichier [dashboard_config.yaml](dashboard_config.yaml) contient deja le mapping par usager:

- `cpapir -> /data/cpapir/reductions`
- `cpapir -> /data/cpapir/dashboard_assets` pour les previews/cache

Optionnel: mettre des overrides locaux dans [dashboard_config.local.yaml](dashboard_config.local.yaml) (ce fichier est ignore par git).

### 5) Lancer

Mode normal (generation + sync rsync si usager `cpapir`):

```bash
python generate_dashboard.py
```

Mode test sans sync:

```bash
python generate_dashboard.py --no-sync
```

## Synchronisation web

- Activee automatiquement uniquement pour les usagers listes dans `sync_enabled_users` (par defaut: `cpapir`)
- Cible/port/exclusions definis dans [dashboard_config.yaml](dashboard_config.yaml)
- Le flag `--no-sync` desactive la sync pour une execution donnee

## Performance (grosse archive)

- Cache persistant des entetes: [dashboard_assets/header_cache.csv](dashboard_assets/header_cache.csv)
- Invalidation cache par mtime + taille de fichier
- Les fichiers `phot_*.fits.gz` sont lus pour les graphes, pas listes dans la table detaillee

## Git et donnees lourdes

Le `.gitignore` exclut deja les donnees scientifiques et produits lourds (`*.fits`, `*.fits.gz`, `*.png`, `*.csv`, etc.).

## Contact

Etienne Artigau: etienne.artigau@umontreal.ca
