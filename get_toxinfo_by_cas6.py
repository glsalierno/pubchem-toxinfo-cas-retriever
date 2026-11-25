import aiohttp
import asyncio
import json
from typing import List, Dict, Any, Optional
import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import logging
from urllib.parse import quote
from openpyxl import Workbook
from openpyxl.utils import get_column_letter
from openpyxl.styles import Font

BASE_URL = "https://pubchem.ncbi.nlm.nih.gov/rest/pug"
VIEW_URL = "https://pubchem.ncbi.nlm.nih.gov/rest/pug_view"

LOG = logging.getLogger(__name__)
HTTP_TIMEOUT = aiohttp.ClientTimeout(total=30)
TRANSIENT_STATUS = {429, 503}
MAX_RETRY_AFTER_SECONDS = 30


def retry_after_seconds(header, default):
    if header:
        try:
            return max(0, int(header))
        except ValueError:
            try:
                deadline = parsedate_to_datetime(header)
            except (ValueError, TypeError, OverflowError):
                return default
            if deadline.tzinfo is not None:
                return max(0, (deadline - datetime.now(timezone.utc)).total_seconds())
    return default


@dataclass
class PubChemPace:
    blocked_until: float = 0.0


async def fetch_url(session: aiohttp.ClientSession, url: str, retries: int = 3,
                    *, pacing: Optional[PubChemPace] = None) -> Dict[str, Any]:
    pacing = pacing if pacing is not None else PubChemPace()
    loop = asyncio.get_running_loop()
    if retries < 1:
        raise ValueError("retries is the maximum number of attempts and must be positive")
    for attempt in range(1, retries + 1):
        remaining = pacing.blocked_until - loop.time()
        if remaining > MAX_RETRY_AFTER_SECONDS:
            return {'error': 'PubChem cooldown active; no request was sent'}
        await asyncio.sleep(max(0.25, remaining))
        try:
            async with session.get(url, timeout=HTTP_TIMEOUT) as response:
                status = response.status
                if status == 200:
                    try:
                        payload = json.loads(await response.text())
                    except (UnicodeDecodeError, json.JSONDecodeError) as e:
                        LOG.error("Invalid PubChem JSON for %s: %s", url, e)
                        return {'error': 'Invalid PubChem JSON response'}
                    if not isinstance(payload, dict):
                        LOG.error("Unexpected PubChem JSON type for %s", url)
                        return {'error': 'Unexpected PubChem JSON response'}
                    return payload
                if status not in TRANSIENT_STATUS:
                    LOG.warning("PubChem HTTP %s for %s", status, url)
                    return {'error': f'HTTP error {status}'}
                delay = max(2 ** (attempt - 1),
                            retry_after_seconds(response.headers.get('Retry-After'), 0))
                pacing.blocked_until = loop.time() + delay
        except asyncio.TimeoutError:
            LOG.error("PubChem request timed out for %s", url)
            return {'error': 'PubChem request timed out'}
        except aiohttp.ClientError as e:
            LOG.error("PubChem request failed for %s: %s", url, e)
            return {'error': f'PubChem request failed: {e}'}

        if attempt == retries or delay > MAX_RETRY_AFTER_SECONDS:
            LOG.warning("PubChem HTTP %s for %s; stopped after %d/%d attempts (cooldown %.1fs)",
                        status, url, attempt, retries, delay)
            return {'error': f'HTTP error {status}'}
        LOG.warning("PubChem HTTP %s for %s after attempt %d/%d; retrying in %.1fs",
                    status, url, attempt, retries, delay)
    raise AssertionError("Unreachable retry loop")


class PubChemLookupError(RuntimeError):
    pass


async def get_pubchem_cid(session: aiohttp.ClientSession, cas_number: str, *, pacing=None) -> Optional[int]:
    url = f"{BASE_URL}/compound/name/{quote(cas_number, safe='')}/cids/JSON"
    data = await fetch_url(session, url, pacing=pacing)
    cids = data.get('IdentifierList', {}).get('CID', [])
    if cids:
        return cids[0]
    if 'error' in data and data['error'] != 'HTTP error 404':
        raise PubChemLookupError(data['error'])
    LOG.warning("No CID returned for CAS %s", cas_number)
    return None

async def get_compound_data(session: aiohttp.ClientSession, cid: int, *, pacing=None) -> Dict[str, Any]:
    url = f"{VIEW_URL}/data/compound/{cid}/JSON"
    return await fetch_url(session, url, pacing=pacing)

async def get_iupac_and_smiles(session: aiohttp.ClientSession, cid: int, *, pacing=None) -> Dict[str, str]:
    url = f"{BASE_URL}/compound/cid/{cid}/property/IUPACName,ConnectivitySMILES/JSON"
    data = await fetch_url(session, url, pacing=pacing)
    rows = data.get('PropertyTable', {}).get('Properties', [])
    if rows:
        properties = rows[0]
        return {
            'IUPAC': properties.get('IUPACName', 'N/A'),
            'SMILES': properties.get('ConnectivitySMILES') or properties.get('CanonicalSMILES', 'N/A')
        }
    return {'IUPAC': 'N/A', 'SMILES': 'N/A'}

def get_names_and_synonyms(data: Dict[str, Any]) -> Dict[str, Any]:
    names_and_synonyms = {'Names': [], 'Synonyms': []}
    if 'Record' in data and 'Section' in data['Record']:
        for section in data['Record']['Section']:
            if section.get('TOCHeading') == 'Names and Identifiers':
                for subsection in section.get('Section', []):
                    if subsection.get('TOCHeading') == 'Computed Descriptors':
                        for info in subsection.get('Information', []):
                            if info.get('Name') == 'IUPAC Name':
                                names_and_synonyms['Names'].append(info['Value']['StringWithMarkup'][0]['String'])
                    elif subsection.get('TOCHeading') == 'Synonyms':
                        for info in subsection.get('Information', []):
                            if info.get('Name') == 'Synonym':
                                names_and_synonyms['Synonyms'].extend([value['String'] for value in info['Value']['StringWithMarkup']])
                break
    return names_and_synonyms

def get_literature_references(data: Dict[str, Any]) -> Dict[str, List[str]]:
    references = {
        'Disease and References': [],
        'Nature Journal References': [],
        'Springer Nature References': [],
        'Other Safety Information': []
    }
    if 'Record' in data and 'Section' in data['Record']:
        for section in data['Record']['Section']:
            if section.get('TOCHeading') in references:
                for info in section.get('Information', []):
                    if 'Value' in info and 'StringWithMarkup' in info['Value']:
                        references[section['TOCHeading']].extend([item['String'] for item in info['Value']['StringWithMarkup']])
    return references

def extract_tox_data(data: Dict[str, Any]) -> Dict[str, Any]:
    tox_data = {}
    if 'Record' not in data or 'Section' not in data['Record']:
        return tox_data

    tox_keywords = ['tox', 'safety', 'hazard', 'health', 'exposure', 'risk', 'carcinogen']
    
    def process_section(section):
        if 'TOCHeading' in section and any(keyword in section['TOCHeading'].lower() for keyword in tox_keywords):
            heading = section['TOCHeading']
            tox_data[heading] = []
            if 'Information' in section:
                for info in section['Information']:
                    if 'Value' in info and 'StringWithMarkup' in info['Value']:
                        tox_data[heading].extend([item['String'] for item in info['Value']['StringWithMarkup']])
            if 'Section' in section:
                for subsection in section['Section']:
                    process_section(subsection)

    for section in data['Record']['Section']:
        process_section(section)

    return tox_data

async def process_cas_number(session: aiohttp.ClientSession, cas_number: str, *, pacing=None) -> Dict[str, Any]:
    LOG.info("Processing CAS %s", cas_number)
    try:
        cid = await get_pubchem_cid(session, cas_number, pacing=pacing)
        if cid is None:
            return {'CAS': cas_number, 'error': 'Could not find PubChem CID'}
        
        LOG.info("Found CID %s for CAS %s", cid, cas_number)
        compound_data = await get_compound_data(session, cid, pacing=pacing)
        if 'error' in compound_data:
            return {'CAS': cas_number, 'PubChemCID': cid, 'error': compound_data['error']}
        
        iupac_smiles = await get_iupac_and_smiles(session, cid, pacing=pacing)
        names_and_synonyms = get_names_and_synonyms(compound_data)
        literature_references = get_literature_references(compound_data)
        
        if 'Record' not in compound_data:
            return {
                'CAS': cas_number,
                'PubChemCID': cid,
                'IUPAC': iupac_smiles['IUPAC'],
                'SMILES': iupac_smiles['SMILES'],
                'Names': names_and_synonyms['Names'],
                'Synonyms': names_and_synonyms['Synonyms'],
                'LiteratureReferences': literature_references,
                'error': 'Could not find compound data'
            }
        
        tox_data = extract_tox_data(compound_data)
        return {
            'CAS': cas_number,
            'PubChemCID': cid,
            'IUPAC': iupac_smiles['IUPAC'],
            'SMILES': iupac_smiles['SMILES'],
            'Names': names_and_synonyms['Names'],
            'Synonyms': names_and_synonyms['Synonyms'],
            'LiteratureReferences': literature_references,
            'ToxData': tox_data
        }
    except PubChemLookupError as e:
        LOG.warning("CAS %s: CID lookup failed: %s", cas_number, e)
        return {'CAS': cas_number, 'error': str(e)}
    except (KeyError, IndexError, TypeError, AttributeError, ValueError) as e:
        LOG.error("CAS %s: unexpected PubChem data: %s", cas_number, e)
        return {'CAS': cas_number, 'error': f"Unexpected PubChem data: {e}"}

async def get_tox_data_for_cas_numbers(cas_numbers: List[str]) -> List[Dict[str, Any]]:
    # Use one paced request stream, so Retry-After applies to the whole batch.
    pacing = PubChemPace()
    async with aiohttp.ClientSession(timeout=HTTP_TIMEOUT) as session:
        return [await process_cas_number(session, cas, pacing=pacing) for cas in cas_numbers]

def save_to_json(data: List[Dict[str, Any]], filename: str):
    with open(filename, 'w') as f:
        json.dump(data, f, indent=2)

def save_to_excel(data: List[Dict[str, Any]], filename: str):
    wb = Workbook()
    wb.remove(wb.active)
    bold_font = Font(bold=True)

    for compound in data:
        cas = compound['CAS']
        ws = wb.create_sheet(title=cas)
        
        ws['A1'], ws['B1'] = 'CAS Number', cas
        ws['A2'], ws['B2'] = 'PubChem CID', compound.get('PubChemCID', 'N/A')
        ws['A3'], ws['B3'] = 'IUPAC Name', compound.get('IUPAC', 'N/A')
        ws['A4'], ws['B4'] = 'SMILES', compound.get('SMILES', 'N/A')

        row = 6
        ws.cell(row=row, column=1, value='Names').font = bold_font
        row += 1
        for name in compound.get('Names', []):
            ws.cell(row=row, column=2, value=name)
            row += 1
        row += 1

        ws.cell(row=row, column=1, value='Synonyms').font = bold_font
        row += 1
        for synonym in compound.get('Synonyms', []):
            ws.cell(row=row, column=2, value=synonym)
            row += 1
        row += 1

        ws.cell(row=row, column=1, value='Literature References').font = bold_font
        row += 1
        for ref_type, references in compound.get('LiteratureReferences', {}).items():
            ws.cell(row=row, column=1, value=ref_type).font = bold_font
            row += 1
            for ref in references:
                ws.cell(row=row, column=2, value=ref)
                row += 1
            row += 1

        ws.cell(row=row, column=1, value='Toxicological Data').font = bold_font
        row += 1

        if 'error' in compound:
            ws.cell(row=row, column=1, value=f"Error: {compound['error']}")
        elif 'ToxData' in compound:
            for category, data in compound['ToxData'].items():
                ws.cell(row=row, column=1, value=category).font = bold_font
                row += 1
                for item in data:
                    ws.cell(row=row, column=2, value=item)
                    row += 1
                row += 1
        else:
            ws.cell(row=row, column=1, value="No toxicological data found")

        for column in ws.columns:
            max_length = 0
            column_letter = get_column_letter(column[0].column)
            for cell in column:
                if cell.value is not None:
                    max_length = max(max_length, len(str(cell.value)))
            adjusted_width = min(max_length + 2, 100)
            ws.column_dimensions[column_letter].width = adjusted_width

    wb.save(filename)
    print(f"Data exported to Excel file: {filename}")

def get_cas_numbers_from_user() -> List[str]:
    print("Enter CAS numbers (one per line). Press Enter twice to finish:")
    cas_numbers = []
    while True:
        try:
            cas = input().strip()
        except EOFError:
            break
        if cas:
            cas_numbers.append(cas)
        else:
            break
    return cas_numbers

def main(argv=None):
    parser = argparse.ArgumentParser(description="Retrieve PubChem toxicology annotations by CAS number.")
    parser.add_argument("cas", nargs="*", metavar="CAS", help="CAS numbers; omit to enter interactively")
    parser.add_argument("--log-level", default="INFO", type=str.upper,
                        choices=("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"),
                        help="stderr diagnostic verbosity (default: INFO)")
    args = parser.parse_args(argv)
    logging.basicConfig(level=args.log_level, format="%(levelname)s: %(message)s")
    cas_numbers = args.cas or get_cas_numbers_from_user()

    if not cas_numbers:
        print("No CAS numbers provided. Exiting.")
        return 1

    print(f"Fetching data for {len(cas_numbers)} CAS numbers...")
    results = asyncio.run(get_tox_data_for_cas_numbers(cas_numbers))
    
    json_filename = "tox_data.json"
    save_to_json(results, json_filename)
    print(f"Data for {len(results)} compounds saved to {json_filename}")

    excel_filename = "tox_data.xlsx"
    save_to_excel(results, excel_filename)

    print("Please check the JSON and Excel files for the retrieved data.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
