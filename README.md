[![MIT License](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Python 3.x](https://img.shields.io/badge/python-3.x-blue.svg)](https://www.python.org/downloads/)

# PubChem Toxicological Info Retriever by CAS

Asynchronous Python script to retrieve toxicological information, IUPAC names, SMILES, synonyms, names, and literature references from PubChem for CAS numbers using PUG REST API. Exports to JSON/Excel.

## Key Features
- Asynchronous client with one paced request stream per batch.
- Extracts tox data, IUPAC/SMILES, synonyms, and literature refs.
- Handles retries for API errors; exports to formatted JSON and Excel.
- Interactive or command-line input.

## Requirements
- Python 3.x
- Install dependencies: `python -m pip install -r requirements.txt`

## Usage
Run with CAS numbers for batch processing:

```bash
python get_toxinfo_by_cas6.py --log-level INFO 67-56-1 64-17-5
```

This writes `tox_data.json` and `tox_data.xlsx` to the current directory,
including error records when a lookup fails. PubChem matches and annotations
are research leads, not independent CAS validation or hazard classifications.

Or run without arguments for interactive mode (enter CAS one per line, blank to finish).

### MATLAB
The MATLAB wrapper calls the Python script and parses the JSON output into a table:

```matlab
toxTable = getToxInfo({'50-00-0', '64-17-5'});
Processed 2 compounds. Table has 2 rows.
```
## Files
- `get_toxinfo_by_cas6.py`: Main script for data retrieval and export.
- getToxInfo.m: MATLAB wrapper function.

## Notes
- Each HTTP attempt has a 30-second total timeout. Rate-limit responses are not treated as unmatched CAS numbers.
- Diagnostics go to stderr; use `--log-level WARNING` for failures and throttling notices only.
- For issues, open a GitHub issue.


Author: glsalierno  
Date: September 2025  
GitHub: [glsalierno](https://github.com/glsalierno)

## Offline tests

```text
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
```

The tests mock PubChem requests; CI does not call PubChem or exercise MATLAB.
Network calls are paced in each process and retry HTTP 429 or 503 at most twice.
The tool honors Retry-After. If PubChem requests a wait over 30 seconds, the
tool does not retry early or issue a new request during that cooldown. Keep
batch sizes modest and follow [PubChem's usage policy](https://pubchem.ncbi.nlm.nih.gov/docs/programmatic-access).
