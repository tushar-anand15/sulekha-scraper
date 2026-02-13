"""
Sulekha Municipal Projects PDF Processor.

Main orchestrator class that ties together:
- PDF utilities (tile creation)
- LLM service (extraction)
- Schemas (structured outputs)
- Prompts (instructions)

Usage:
    processor = SulekhaProcessor(api_key="your-gemini-api-key")
    result = await processor.process_pdf(pdf_bytes)
    print(result.projects)
"""

import asyncio
import json
import logging
from pathlib import Path
from typing import List, Optional, Dict, Any

from pdf_utils import (
    create_all_grid_tiles,
    get_page_count,
    save_tile_to_file,
)
from llm_service import LLMService, create_llm_service
from schemas import (
    SulekhaTileExtraction,
    FullSulekhaExtraction,
    MunicipalProject,
    merge_tile_extractions,
)
from prompts import (
    SULEKHA_EXTRACTION_SYSTEM_PROMPT,
    get_sulekha_tile_extraction_prompt,
)

logger = logging.getLogger(__name__)


class SulekhaProcessor:
    """
    Main processor for extracting municipal project data from Sulekha PDFs.
    
    Features:
    - Converts PDFs to 2x2 grid tiles for efficient VLM processing
    - Extracts structured project data using Gemini
    - Merges and deduplicates results across tiles
    - Provides usage statistics and cost tracking
    """
    
    def __init__(
        self,
        api_key: str,
        model: str = "gemini-2.5-flash",
        output_dir: Optional[Path] = None,
        save_tiles: bool = False,
    ):
        """
        Initialize the processor.
        
        Args:
            api_key: Gemini API key
            model: Model to use for extraction (default: gemini-2.5-flash)
            output_dir: Directory to save debug output (tiles, raw responses)
            save_tiles: Whether to save tile images to disk for debugging
        """
        self.llm_service = create_llm_service(api_key)
        self.model = model
        self.output_dir = Path(output_dir) if output_dir else None
        self.save_tiles = save_tiles
        
        # Processing state
        self._pdf_bytes: Optional[bytes] = None
        self._total_pages: int = 0
        self._tiles: List[tuple] = []
        
        if self.output_dir:
            self.output_dir.mkdir(parents=True, exist_ok=True)
    
    async def process_pdf(
        self,
        pdf_bytes: bytes,
        parallel: bool = False,
    ) -> FullSulekhaExtraction:
        """
        Process a PDF and extract municipal project data.
        
        Args:
            pdf_bytes: Raw PDF file bytes
            parallel: Whether to process tiles in parallel (faster but may hit rate limits)
        
        Returns:
            FullSulekhaExtraction with all extracted projects
        """
        logger.info("[PROCESSOR] Starting PDF processing...")
        
        # Store PDF bytes
        self._pdf_bytes = pdf_bytes
        self._total_pages = get_page_count(pdf_bytes)
        
        logger.info(f"[PROCESSOR] PDF has {self._total_pages} pages")
        
        # Create tiles
        logger.info("[PROCESSOR] Creating tiles...")
        self._tiles = create_all_grid_tiles(pdf_bytes, add_page_labels=True)
        
        logger.info(f"[PROCESSOR] Created {len(self._tiles)} tiles")
        
        # Save tiles if debugging
        if self.save_tiles and self.output_dir:
            for i, (tile_bytes, page_nums) in enumerate(self._tiles):
                save_tile_to_file(
                    tile_bytes,
                    self.output_dir / f"tile_{i+1}_pages_{'-'.join(map(str, page_nums))}.jpg"
                )
        
        # Process tiles
        if parallel:
            tile_results = await self._process_tiles_parallel()
        else:
            tile_results = await self._process_tiles_sequential()
        
        # Merge results
        logger.info("[PROCESSOR] Merging tile results...")
        result = merge_tile_extractions(tile_results)
        
        # Log stats
        stats = self.llm_service.get_usage_stats()
        logger.info(f"[PROCESSOR] Extraction complete. Stats: {json.dumps(stats, indent=2)}")
        
        # Save final result if debugging
        if self.output_dir:
            with open(self.output_dir / "extraction_result.json", "w") as f:
                json.dump(result.model_dump(), f, indent=2)
        
        return result
    
    async def _process_tiles_sequential(self) -> List[SulekhaTileExtraction]:
        """Process tiles one at a time (safer for rate limits)."""
        results = []
        
        for i, (tile_bytes, page_numbers) in enumerate(self._tiles):
            tile_number = i + 1
            is_first = (i == 0)
            
            logger.info(f"[PROCESSOR] Processing tile {tile_number}/{len(self._tiles)}...")
            
            result = await self._extract_tile(
                tile_bytes=tile_bytes,
                page_numbers=page_numbers,
                tile_number=tile_number,
                is_first_tile=is_first,
            )
            
            if result:
                results.append(result)
                logger.info(f"[PROCESSOR] Tile {tile_number}: extracted {len(result.projects)} projects")
            else:
                logger.warning(f"[PROCESSOR] Tile {tile_number}: no result")
        
        return results
    
    async def _process_tiles_parallel(self) -> List[SulekhaTileExtraction]:
        """Process tiles in parallel (faster but may hit rate limits)."""
        tasks = []
        
        for i, (tile_bytes, page_numbers) in enumerate(self._tiles):
            task = self._extract_tile(
                tile_bytes=tile_bytes,
                page_numbers=page_numbers,
                tile_number=i + 1,
                is_first_tile=(i == 0),
            )
            tasks.append(task)
        
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        # Filter out errors and None results
        valid_results = []
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                logger.error(f"[PROCESSOR] Tile {i+1} failed: {result}")
            elif result is not None:
                valid_results.append(result)
        
        return valid_results
    
    async def _extract_tile(
        self,
        tile_bytes: bytes,
        page_numbers: List[int],
        tile_number: int,
        is_first_tile: bool = False,
    ) -> Optional[SulekhaTileExtraction]:
        """
        Extract project data from a single tile.
        
        Args:
            tile_bytes: JPEG image bytes of the tile
            page_numbers: Page numbers included in this tile
            tile_number: Tile number (1-indexed)
            is_first_tile: Whether this is the first tile
        
        Returns:
            SulekhaTileExtraction or None if extraction failed
        """
        # Build prompt
        prompt = get_sulekha_tile_extraction_prompt(
            tile_number=tile_number,
            page_numbers=page_numbers,
            total_tiles=len(self._tiles),
            total_pages=self._total_pages,
            is_first_tile=is_first_tile,
        )
        
        # Save prompt for debugging
        if self.output_dir:
            with open(self.output_dir / f"prompt_tile_{tile_number}.txt", "w") as f:
                f.write(prompt)
        
        try:
            result = await self.llm_service.chat(
                model=self.model,
                user_prompt=prompt,
                system_prompt=SULEKHA_EXTRACTION_SYSTEM_PROMPT,
                images=[tile_bytes],
                structured_output_schema=SulekhaTileExtraction,
                temperature=0.1,  # Low temperature for consistent extraction
            )
            
            if isinstance(result, SulekhaTileExtraction):
                # Save raw result for debugging
                if self.output_dir:
                    with open(self.output_dir / f"result_tile_{tile_number}.json", "w") as f:
                        json.dump(result.model_dump(), f, indent=2)
                
                return result
            
            logger.warning(f"[PROCESSOR] Tile {tile_number}: unexpected result type {type(result)}")
            return None
            
        except Exception as e:
            logger.error(f"[PROCESSOR] Tile {tile_number} extraction failed: {e}")
            raise
    
    def get_usage_stats(self) -> Dict[str, Any]:
        """Get LLM usage statistics."""
        return self.llm_service.get_usage_stats()


# =============================================================================
# Convenience Functions
# =============================================================================


async def process_pdf_file(
    pdf_path: Path,
    api_key: str,
    output_dir: Optional[Path] = None,
    save_tiles: bool = False,
) -> FullSulekhaExtraction:
    """
    Convenience function to process a PDF file.
    
    Args:
        pdf_path: Path to the PDF file
        api_key: Gemini API key
        output_dir: Optional directory for debug output
        save_tiles: Whether to save tile images
    
    Returns:
        FullSulekhaExtraction result
    """
    pdf_bytes = Path(pdf_path).read_bytes()
    
    processor = SulekhaProcessor(
        api_key=api_key,
        output_dir=output_dir,
        save_tiles=save_tiles,
    )
    
    return await processor.process_pdf(pdf_bytes)


def process_pdf_file_sync(
    pdf_path: Path,
    api_key: str,
    output_dir: Optional[Path] = None,
    save_tiles: bool = False,
) -> FullSulekhaExtraction:
    """Synchronous version of process_pdf_file."""
    return asyncio.run(process_pdf_file(
        pdf_path=pdf_path,
        api_key=api_key,
        output_dir=output_dir,
        save_tiles=save_tiles,
    ))


# =============================================================================
# CLI Entry Point
# =============================================================================


if __name__ == "__main__":
    import argparse
    import os
    
    parser = argparse.ArgumentParser(description="Extract municipal project data from Sulekha PDFs")
    parser.add_argument("pdf_path", help="Path to PDF file")
    parser.add_argument("--api-key", default=os.environ.get("GEMINI_API_KEY"), help="Gemini API key")
    parser.add_argument("--output-dir", help="Directory for debug output")
    parser.add_argument("--save-tiles", action="store_true", help="Save tile images")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    
    args = parser.parse_args()
    
    if not args.api_key:
        print("Error: API key required. Set GEMINI_API_KEY env var or use --api-key")
        exit(1)
    
    # Configure logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s"
    )
    
    # Process
    result = process_pdf_file_sync(
        pdf_path=Path(args.pdf_path),
        api_key=args.api_key,
        output_dir=Path(args.output_dir) if args.output_dir else None,
        save_tiles=args.save_tiles,
    )
    
    # Output
    if args.json:
        print(json.dumps(result.model_dump(), indent=2))
    else:
        print(f"\n{'='*60}")
        print(f"EXTRACTION RESULT")
        print(f"{'='*60}")
        print(f"Total Pages: {result.total_pages}")
        print(f"Total Projects: {result.total_projects}")
        if result.total_cost:
            print(f"Total Cost: ₹{result.total_cost:,.2f}")
        print(f"\nProjects:")
        print("-" * 60)
        for i, project in enumerate(result.projects, 1):
            cost_str = f"₹{project.project_cost:,.2f}" if project.project_cost else "N/A"
            print(f"{i}. {project.project_name}")
            print(f"   Cost: {cost_str} | Page: {project.source_page}")
        
        if result.warnings:
            print(f"\nWarnings:")
            for warning in result.warnings:
                print(f"  - {warning}")
