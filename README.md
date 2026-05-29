# Technician Command Center

A Flask and SQLite diagnostic toolkit for IT technicians that captures endpoint, network, service, printer, and performance snapshots, compares before/after troubleshooting states, and exports support-ready reports.

## Project Purpose

Technician Command Center is designed to demonstrate real IT troubleshooting thinking, not just a basic dashboard. The app helps a technician capture evidence from an endpoint, review issue-focused diagnostics, compare before/after snapshots, and generate reports that can be used for documentation or escalation.

The project supports two modes:

- **Local Mode:** collects real diagnostic information from the machine running the app.
- **Demo Mode:** uses safe sample diagnostic data for a public portfolio demo.

This design keeps the public version safe while still showing the real purpose of the tool.

## Features

- Flask web application with SQLite storage
- Local endpoint snapshot collection
- Demo scenarios for portfolio walkthroughs
- Network diagnostics
- Service diagnostics
- Printer diagnostics, including offline printer detection
- Endpoint performance diagnostics
- Issue-focused snapshot views
- Technician case details and notes
- Smart before/after snapshot comparison
- TXT, JSON, printable HTML, and ZIP support-bundle exports
- Portfolio demo seed and clear controls
- Deployment guide and portfolio readiness page
- Demo-only mode for public hosting

## Diagnostic Areas

The app can collect or display information related to:

- Hostname, logged-in user, OS, Python version
- Local IP, default gateway, DNS servers, DNS lookup, ping checks, web connectivity
- Print Spooler and other Windows services
- Installed printers, default printer, offline printers, and queued jobs
- Disk space and storage status
- Uptime, boot time, memory, process count, and recent system events

## Tech Stack

- Python
- Flask
- SQLite
- HTML/CSS rendered through Flask templates
- Windows PowerShell/CIM commands for selected local diagnostics

## Project Structure

```text
technician-command-center/
├── app.py
├── requirements.txt
├── README.md
├── .gitignore
└── data/                # Created automatically at runtime; not uploaded to GitHub
```

## Setup Instructions

### 1. Clone or download the project

```bash
git clone <your-repository-url>
cd technician-command-center
```

### 2. Create a virtual environment

Windows PowerShell:

```powershell
python -m venv venv
venv\Scripts\activate
```

macOS/Linux:

```bash
python3 -m venv venv
source venv/bin/activate
```

### 3. Install dependencies

```bash
python -m pip install -r requirements.txt
```

### 4. Run the app

```bash
python app.py
```

Then open:

```text
http://127.0.0.1:5000
```

## Public Demo Mode

For public hosting, use demo-only mode so the hosted version does not attempt to collect diagnostics from the hosting server.

Windows PowerShell:

```powershell
$env:TCC_DEMO_ONLY="1"
python app.py
```

macOS/Linux:

```bash
export TCC_DEMO_ONLY=1
python app.py
```

When demo-only mode is enabled:

- Local Mode is disabled.
- Demo Mode remains available.
- Seeded portfolio demo snapshots can be used for walkthroughs.
- Recruiters can still test the snapshot, comparison, and export workflow safely.

## Suggested Demo Walkthrough

1. Open the Portfolio Demo page.
2. Seed demo data.
3. Open a DNS failure snapshot.
4. Open the issue-focused diagnostic view.
5. Compare DNS Failure before/after with Healthy Endpoint.
6. Download the ZIP support bundle.
7. Open the printable HTML report.
8. Explain that Local Mode performs real diagnostics when run on a technician machine.

## Screenshot Checklist

Recommended screenshots for a portfolio page:

- Dashboard with snapshot metrics
- Run Snapshot page showing Demo Mode and Local Mode
- Local snapshot detail page
- Printer diagnostics section
- Endpoint performance diagnostics section
- Smart Compare page
- Reports/export page
- Portfolio Demo page
- Printable HTML report

## Resume Bullet Options

- Built a Flask and SQLite diagnostic toolkit for IT technicians to capture endpoint, network, service, printer, and performance snapshots.
- Implemented before/after diagnostic comparison to help validate troubleshooting outcomes and identify changes between endpoint states.
- Added report exports in TXT, JSON, printable HTML, and ZIP support-bundle formats for documentation and escalation.
- Designed public demo mode with seeded troubleshooting scenarios while preserving local mode for real endpoint diagnostics.

## Portfolio Description

Technician Command Center is a local diagnostic and reporting tool for IT support technicians. It captures endpoint health, network status, service state, printer status, and performance indicators, then saves the results as snapshots. Technicians can compare before/after states, document case details, and export support-ready reports. The public demo uses safe sample data, while the local version performs real diagnostics on the machine running the app.

## Important Privacy Note

Do not upload local database files to GitHub. Local snapshots may include your hostname, username, printer names, local IP information, and other machine-specific details.

The `.gitignore` file excludes:

```text
data/
*.db
venv/
__pycache__/
```

## Status

Version 1 portfolio-ready.
