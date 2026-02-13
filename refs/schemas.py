"""
Pydantic Schemas for Sulekha Municipal Project Extraction.

These schemas define the structure of data extracted from municipal PDFs.
Customize the MunicipalProject fields based on the actual PDF content.
"""

from __future__ import annotations

from typing import Dict, List, Optional

from pydantic import BaseModel, Field


# =============================================================================
# Core Municipal Project Schemas
# =============================================================================


class MunicipalProject(BaseModel):
    """
    A single municipal project extracted from a Sulekha PDF.
    
    Customize these fields based on what's actually in the PDFs:
    - Add fields for data present in the documents
    - Make fields Optional if not always present
    - Use appropriate types (str, float, int, etc.)
    """
    
    project_name: str = Field(
        description="Full name/title of the municipal project"
    )
    project_cost: Optional[float] = Field(
        default=None,
        description="Total cost of the project (normalized to base currency unit)"
    )
    cost_unit: str = Field(
        default="INR",
        description="Currency unit: 'INR' (rupees), 'lakhs', 'crores'"
    )
    cost_raw: Optional[str] = Field(
        default=None,
        description="Original cost string as it appears in document (e.g., '₹2.5 Cr')"
    )
    source_page: int = Field(
        description="Page number where this project appears (1-indexed)"
    )
    
    # Project classification fields
    project_category: Optional[str] = Field(
        default=None,
        description="Category of project: 'Infrastructure', 'Water Supply', 'Sanitation', 'Roads', 'Buildings', 'Agriculture', 'Health', 'Education', 'Welfare', 'Other'"
    )
    main_area: Optional[str] = Field(
        default=None,
        description="Main area/domain covered by the project (e.g., 'Drinking Water', 'Road Construction', 'School Building', 'Drainage System')"
    )
    sector: Optional[str] = Field(
        default=None,
        description="High-level sector/region (e.g., 'Public Works', 'Rural Development', 'Urban Development', 'Social Welfare')"
    )
    sub_sector: Optional[str] = Field(
        default=None,
        description="Sub-sector/sub-region within the main sector (e.g., 'Water Infrastructure', 'Road Maintenance', 'Primary Education')"
    )
    micro_sector: Optional[str] = Field(
        default=None,
        description="Micro-sector/micro-region - most specific classification (e.g., 'Pipeline Extension', 'Bridge Repair', 'Anganwadi Construction')"
    )


class PageInfo(BaseModel):
    """Information about a single page in the tile."""
    
    page_number: int = Field(
        description="Page number (1-indexed)"
    )
    projects_on_page: List[str] = Field(
        default_factory=list,
        description="Project names found on this page"
    )
    has_table: bool = Field(
        default=False,
        description="Whether this page contains tabular data"
    )
    notes: Optional[str] = Field(
        default=None,
        description="Any observations about this page"
    )


# =============================================================================
# Tile Extraction Schemas
# =============================================================================


class SulekhaTileExtraction(BaseModel):
    """
    Extraction result from a single tile (up to 4 pages).
    
    This is what the LLM returns for each tile during processing.
    """
    
    tile_number: int = Field(
        description="Tile number (1-indexed)"
    )
    page_numbers: List[int] = Field(
        description="Page numbers included in this tile (1-indexed)"
    )
    pages: List[PageInfo] = Field(
        default_factory=list,
        description="Information about each page in the tile"
    )
    projects: List[MunicipalProject] = Field(
        default_factory=list,
        description="All projects extracted from this tile"
    )
    document_metadata: Optional[Dict] = Field(
        default=None,
        description="Document-level metadata (only from first tile): title, date, issuing_authority"
    )
    notes: Optional[str] = Field(
        default=None,
        description="Any observations about data quality, format issues, or unclear content"
    )


# =============================================================================
# Complete Extraction Result
# =============================================================================


class FullSulekhaExtraction(BaseModel):
    """
    Complete extraction result after processing all tiles.
    
    This is the final output combining results from all tiles.
    """
    
    total_pages: int = Field(
        description="Total number of pages in the PDF"
    )
    total_projects: int = Field(
        description="Total number of projects extracted"
    )
    document_title: Optional[str] = Field(
        default=None,
        description="Title of the document if detected"
    )
    document_date: Optional[str] = Field(
        default=None,
        description="Date of the document if detected"
    )
    issuing_authority: Optional[str] = Field(
        default=None,
        description="Issuing authority/department if detected"
    )
    projects: List[MunicipalProject] = Field(
        default_factory=list,
        description="All extracted projects (deduplicated)"
    )
    extraction_summary: str = Field(
        default="",
        description="Summary of the extraction process"
    )
    warnings: List[str] = Field(
        default_factory=list,
        description="Any warnings or issues encountered during extraction"
    )
    
    # Statistics
    total_cost: Optional[float] = Field(
        default=None,
        description="Sum of all project costs (if calculable)"
    )
    cost_breakdown_by_category: Optional[Dict[str, float]] = Field(
        default=None,
        description="Cost breakdown by project category"
    )


# =============================================================================
# Utility Functions
# =============================================================================


def normalize_cost(cost_raw: str) -> tuple[Optional[float], str]:
    """
    Normalize a cost string to a numeric value.
    
    Args:
        cost_raw: Raw cost string like "₹2.5 Cr", "25 lakhs", "₹50,000"
    
    Returns:
        Tuple of (normalized_value, unit)
    
    Example:
        >>> normalize_cost("₹2.5 Cr")
        (25000000.0, "INR")
        >>> normalize_cost("25 lakhs")
        (2500000.0, "INR")
    """
    import re
    
    if not cost_raw:
        return None, "INR"
    
    # Clean the string
    cost_str = cost_raw.lower().strip()
    
    # Remove currency symbols
    cost_str = re.sub(r'[₹$€£]', '', cost_str)
    cost_str = re.sub(r'rs\.?', '', cost_str, flags=re.IGNORECASE)
    
    # Extract numeric value
    numbers = re.findall(r'[\d,]+\.?\d*', cost_str)
    if not numbers:
        return None, "INR"
    
    value = float(numbers[0].replace(',', ''))
    
    # Check for multipliers
    if 'cr' in cost_str or 'crore' in cost_str:
        value *= 10_000_000  # 1 crore = 10 million
    elif 'lakh' in cost_str or 'lac' in cost_str:
        value *= 100_000  # 1 lakh = 100 thousand
    elif 'k' in cost_str or 'thousand' in cost_str:
        value *= 1_000
    
    return value, "INR"


def deduplicate_projects(projects: List[MunicipalProject]) -> List[MunicipalProject]:
    """
    Remove duplicate projects based on project name similarity.
    
    Uses simple exact matching - can be enhanced with fuzzy matching if needed.
    """
    seen = set()
    unique = []
    
    for project in projects:
        # Normalize name for comparison
        key = project.project_name.lower().strip()
        
        if key not in seen:
            seen.add(key)
            unique.append(project)
    
    return unique


def merge_tile_extractions(
    tile_results: List[SulekhaTileExtraction]
) -> FullSulekhaExtraction:
    """
    Merge results from multiple tiles into a single extraction result.
    
    Args:
        tile_results: List of extraction results from each tile
    
    Returns:
        Merged FullSulekhaExtraction
    """
    all_projects = []
    all_pages = set()
    warnings = []
    document_metadata = None
    
    for tile in tile_results:
        all_projects.extend(tile.projects)
        all_pages.update(tile.page_numbers)
        
        if tile.notes:
            warnings.append(f"Tile {tile.tile_number}: {tile.notes}")
        
        # Get document metadata from first tile
        if tile.document_metadata and document_metadata is None:
            document_metadata = tile.document_metadata
    
    # Deduplicate projects
    unique_projects = deduplicate_projects(all_projects)
    
    # Calculate total cost if possible
    total_cost = None
    try:
        costs = [p.project_cost for p in unique_projects if p.project_cost is not None]
        if costs:
            total_cost = sum(costs)
    except:
        pass
    
    return FullSulekhaExtraction(
        total_pages=len(all_pages),
        total_projects=len(unique_projects),
        document_title=document_metadata.get('title') if document_metadata else None,
        document_date=document_metadata.get('date') if document_metadata else None,
        issuing_authority=document_metadata.get('issuing_authority') if document_metadata else None,
        projects=unique_projects,
        extraction_summary=f"Extracted {len(unique_projects)} projects from {len(all_pages)} pages",
        warnings=warnings,
        total_cost=total_cost,
    )
