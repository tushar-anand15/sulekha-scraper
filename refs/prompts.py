"""
Prompts for Sulekha Municipal Project Extraction.

Contains prompt templates for the VLM to extract project data from municipal PDFs.
"""

from typing import List, Dict, Any, Optional


# =============================================================================
# System Prompts
# =============================================================================


SULEKHA_EXTRACTION_SYSTEM_PROMPT = """You are an expert data extractor specializing in Indian municipal government documents.

Your task is to extract PUBLIC WORKS PROJECT information from Sulekha municipal PDFs.

## PRIMARY EXTRACTION TARGETS:
1. **Project Name**: The full official name/title of the public works project
2. **Project Cost**: The total cost/budget allocated (in INR - may be in lakhs/crores)

## PROJECT CLASSIFICATION (extract for each project):
3. **project_category**: High-level category. Choose from:
   - Infrastructure, Water Supply, Sanitation, Roads, Buildings, Agriculture, Health, Education, Welfare, Other

4. **main_area**: Main domain/area covered (e.g., "Drinking Water", "Road Construction", "School Building", "Drainage System", "Pipeline Extension")

5. **sector**: High-level sector/region:
   - Public Works, Rural Development, Urban Development, Social Welfare, Agriculture & Irrigation, Health & Sanitation, Education, Housing, Energy, Transport, Other

6. **sub_sector**: Sub-sector within main sector (e.g., "Water Infrastructure", "Road Maintenance", "Primary Education", "Rural Housing")

7. **micro_sector**: Most specific classification (e.g., "Pipeline Extension", "Bridge Repair", "Anganwadi Construction", "Street Lighting")

## COST EXTRACTION GUIDELINES:
- Recognize Indian number formats: "₹2.5 Cr", "Rs. 25 lakhs", "₹50,000"
- 1 Crore = 10,000,000 (1 Cr, Crore, Crores)
- 1 Lakh = 100,000 (L, Lac, Lakh, Lakhs)
- Preserve the raw cost string AND provide normalized value
- If cost has range (e.g., "₹2-3 Cr"), use the higher value

## DATA QUALITY GUIDELINES:
- Extract ALL projects visible in the tile
- If project spans multiple rows/columns in a table, extract as single entry
- If data is unclear/illegible, set field to null and add a note
- Preserve exact project names as written (don't paraphrase)
- If same project appears multiple times, extract only once
- Infer classification from project name/context if not explicitly stated

## TABLE DETECTION:
- Municipal documents often have tabular data
- Column headers may include: S.No, Project Name, Amount, Contractor, Status, Ward
- Extract data row by row, mapping to appropriate fields

## OUTPUT FORMAT:
Return valid JSON matching the provided schema. Be thorough but precise.
"""


# =============================================================================
# Dynamic Prompt Builders
# =============================================================================


def get_sulekha_tile_extraction_prompt(
    tile_number: int,
    page_numbers: List[int],
    total_tiles: int,
    total_pages: int,
    is_first_tile: bool = False,
) -> str:
    """
    Generate the extraction prompt for a single tile.
    
    Args:
        tile_number: Current tile number (1-indexed)
        page_numbers: List of page numbers in this tile
        total_tiles: Total number of tiles
        total_pages: Total number of pages in the document
        is_first_tile: Whether this is the first tile (extract metadata)
    
    Returns:
        Prompt string for the LLM
    """
    pages_str = ", ".join(str(p) for p in page_numbers)
    
    # Build page layout description
    layout_lines = ["**PAGE LAYOUT** (2x2 grid, each quadrant labeled):"]
    positions = ["Top-left", "Top-right", "Bottom-left", "Bottom-right"]
    for i, pos in enumerate(positions):
        if i < len(page_numbers):
            layout_lines.append(f"- {pos}: Page {page_numbers[i]}")
        else:
            layout_lines.append(f"- {pos}: (empty/white)")
    
    layout = "\n".join(layout_lines)
    
    # First tile instructions for document metadata
    first_tile_instructions = ""
    if is_first_tile:
        first_tile_instructions = """
**FIRST TILE - DOCUMENT METADATA EXTRACTION:**
This tile contains the first page(s), typically including the document header.

Extract into document_metadata:
1. **title**: Document title (e.g., "List of Public Works Projects 2024-25")
2. **date**: Document date if visible
3. **issuing_authority**: Department/authority name (e.g., "Sulekha Municipal Corporation")

Only extract metadata from THIS tile - other tiles should set document_metadata to null.
"""
    
    return f"""Extract municipal project data from this Sulekha document image.

**CONTEXT:**
- Tile {tile_number} of {total_tiles}
- Pages in this tile: {pages_str}
- Total document pages: {total_pages}

{layout}
{first_tile_instructions}
**EXTRACTION TASKS:**

1. **Page Analysis**: For each visible page, note:
   - Which projects appear on that page
   - Whether data is in tabular or list format
   - Any headers or section titles

2. **Project Extraction**: For each project visible, extract:
   - project_name: Full official name (exact as written)
   - project_cost: Numeric value (normalized to rupees if possible)
   - cost_unit: "INR", "lakhs", or "crores" (whichever is original)
   - cost_raw: Original cost string as it appears (e.g., "₹2.5 Cr")
   - source_page: Page number where this project appears
   - project_category: Category (Infrastructure/Water Supply/Sanitation/Roads/Buildings/Agriculture/Health/Education/Welfare/Other)
   - main_area: Main domain covered (e.g., "Drinking Water", "Road Construction")
   - sector: High-level sector (Public Works/Rural Development/Urban Development/Social Welfare/etc.)
   - sub_sector: Sub-sector within main sector
   - micro_sector: Most specific classification

3. **Notes**: Add any observations about:
   - Data quality issues
   - Unclear or illegible content
   - Format inconsistencies

**IMPORTANT:**
- Extract EVERY project visible in the tile
- Do NOT skip any rows in tables
- If a project spans pages, include it only once
- Preserve exact project names (no paraphrasing)

Return all projects found in this tile as structured JSON."""


def get_sulekha_preprocessing_prompt(
    tile_number: int,
    page_numbers: List[int],
    total_tiles: int,
    total_pages: int,
) -> str:
    """
    Generate a preprocessing/detection prompt for initial document analysis.
    
    Use this for a quick first pass to understand document structure
    before detailed extraction.
    """
    pages_str = ", ".join(str(p) for p in page_numbers)
    
    return f"""Analyze this municipal document image for structure (Tile {tile_number}/{total_tiles}).

Pages in this tile: {pages_str}

**DETECTION TASKS:**

1. **Document Type**: What type of municipal document is this?
   - Project list/catalog
   - Budget document
   - Tender notice
   - Progress report
   - Other (describe)

2. **Data Format**: How is data organized?
   - Tabular (describe columns)
   - List format
   - Mixed format
   - Narrative text

3. **Content Inventory**: What types of information are visible?
   - Project names: Yes/No
   - Cost figures: Yes/No
   - Contractor names: Yes/No
   - Dates: Yes/No
   - Status information: Yes/No
   - Ward/location info: Yes/No

4. **Quality Assessment**:
   - Scan quality: Good/Fair/Poor
   - Text readability: Clear/Partial/Difficult
   - Any visible issues

5. **Project Count Estimate**: Approximately how many projects are visible?

Return your analysis as structured JSON."""


def get_cost_normalization_prompt(
    costs: List[str],
) -> str:
    """
    Generate a prompt to normalize a batch of cost strings.
    
    Useful if you want to batch-process costs separately.
    """
    costs_list = "\n".join(f"- \"{cost}\"" for cost in costs)
    
    return f"""Normalize the following Indian currency cost strings to rupees.

**Cost Strings:**
{costs_list}

**Conversion Rules:**
- 1 Crore = 10,000,000 rupees
- 1 Lakh = 100,000 rupees
- Remove currency symbols (₹, Rs.)
- Handle ranges by using the higher value
- Return null if unparseable

**Output Format:**
Return a JSON array with objects containing:
- original: The original string
- value_inr: Numeric value in rupees (or null)
- confidence: "high", "medium", or "low"

Example:
[
  {{"original": "₹2.5 Cr", "value_inr": 25000000, "confidence": "high"}},
  {{"original": "approx 50L", "value_inr": 5000000, "confidence": "medium"}}
]"""


# =============================================================================
# Example Prompts for Different Document Types
# =============================================================================


def get_tender_document_prompt(
    tile_number: int,
    page_numbers: List[int],
    total_tiles: int,
) -> str:
    """Specialized prompt for tender/bid documents."""
    pages_str = ", ".join(str(p) for p in page_numbers)
    
    return f"""Extract tender/bid information from this municipal document (Tile {tile_number}/{total_tiles}).

Pages: {pages_str}

**EXTRACTION TARGETS:**
1. Tender ID/Number
2. Project Name/Description
3. Estimated Cost
4. EMD (Earnest Money Deposit) amount
5. Bid submission deadline
6. Opening date
7. Eligible contractor categories

Focus on extracting structured tender data.
Return as JSON matching the provided schema."""


def get_budget_document_prompt(
    tile_number: int,
    page_numbers: List[int],
    total_tiles: int,
) -> str:
    """Specialized prompt for budget allocation documents."""
    pages_str = ", ".join(str(p) for p in page_numbers)
    
    return f"""Extract budget allocation data from this municipal document (Tile {tile_number}/{total_tiles}).

Pages: {pages_str}

**EXTRACTION TARGETS:**
1. Department/Head name
2. Budget head code (if visible)
3. Allocated amount
4. Expenditure (if shown)
5. Balance (if shown)
6. Financial year
7. Category (capital/revenue)

Focus on extracting financial allocation data.
Return as JSON matching the provided schema."""
