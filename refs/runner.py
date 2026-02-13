#!/usr/bin/env python3
"""
Sulekha PDF Extraction Runner

This script processes all PDFs from a source directory, extracts municipal project
data using Gemini's vision API, and outputs the results as JSON files.

Usage:
    # Set your API key first:
    export GEMINI_API_KEY="your-api-key-here"
    
    # Or create a .env file with:
    # GEMINI_API_KEY=your-api-key-here
    
    # Run the script:
    python runner.py
    
    # Or specify a custom PDF directory:
    python runner.py --pdf-dir /path/to/pdfs
    
    # Process a single PDF:
    python runner.py --single path/to/file.pdf

Output:
    - Individual JSON files for each PDF in the output/ directory
    - A combined results.json with all extractions
    - Console summary of all processed files
"""

import asyncio
import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Dict, Any

# Try to load environment variables from .env file
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # python-dotenv not installed, will use environment variables directly

from pdf_utils import create_grid_tile, convert_pdf_to_images, get_page_count
from llm_service import create_llm_service
from schemas import SulekhaTileExtraction, FullSulekhaExtraction, MunicipalProject
from prompts import SULEKHA_EXTRACTION_SYSTEM_PROMPT, get_sulekha_tile_extraction_prompt

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
    ]
)
logger = logging.getLogger(__name__)


# =============================================================================
# Configuration
# =============================================================================

DEFAULT_PDF_DIR = Path(__file__).parent / "sulekha_notes" / "pdfs"
DEFAULT_OUTPUT_DIR = Path(__file__).parent / "output"
DEFAULT_MODEL = "gemini-2.5-flash"


# =============================================================================
# Runner Class
# =============================================================================

class SulekhaRunner:
    """
    Batch runner for processing multiple Sulekha PDFs in parallel.
    
    Features:
    - Processes only first tile (4 pages) per PDF for speed
    - Processes all PDFs in parallel
    - Saves individual JSON results for each PDF
    - Creates a combined summary JSON
    """
    
    def __init__(
        self,
        api_key: str,
        pdf_dir: Path = DEFAULT_PDF_DIR,
        output_dir: Path = DEFAULT_OUTPUT_DIR,
        model: str = DEFAULT_MODEL,
        save_tiles: bool = False,
    ):
        """
        Initialize the runner.
        
        Args:
            api_key: Gemini API key
            pdf_dir: Directory containing PDFs to process
            output_dir: Directory to save results
            model: Gemini model to use
            save_tiles: Whether to save tile images for debugging
        """
        self.api_key = api_key
        self.pdf_dir = Path(pdf_dir)
        self.output_dir = Path(output_dir)
        self.model = model
        self.save_tiles = save_tiles
        self.llm_service = create_llm_service(api_key)
        
        # Create output directory
        self.output_dir.mkdir(parents=True, exist_ok=True)
        
        # Processing statistics
        self.stats = {
            "start_time": None,
            "end_time": None,
            "total_pdfs": 0,
            "successful": 0,
            "failed": 0,
            "total_projects": 0,
            "total_pages": 0,
            "errors": [],
        }
    
    def get_pdf_files(self) -> List[Path]:
        """Get all PDF files in the source directory."""
        if not self.pdf_dir.exists():
            logger.error(f"PDF directory not found: {self.pdf_dir}")
            return []
        
        pdf_files = list(self.pdf_dir.glob("*.pdf"))
        logger.info(f"Found {len(pdf_files)} PDF files in {self.pdf_dir}")
        return sorted(pdf_files)
    
    async def process_single_pdf(
        self,
        pdf_path: Path,
    ) -> Optional[Dict[str, Any]]:
        """
        Process a single PDF file - only first tile (4 pages).
        
        Args:
            pdf_path: Path to the PDF file
        
        Returns:
            Extraction result as dict, or None if failed
        """
        logger.info(f"Processing: {pdf_path.name}")
        
        try:
            # Read PDF bytes
            pdf_bytes = pdf_path.read_bytes()
            
            # Convert PDF to images
            pages = convert_pdf_to_images(pdf_bytes)
            total_pages = len(pages)
            
            if not pages:
                logger.warning(f"No pages found in {pdf_path.name}")
                return None
            
            # Create only ONE tile from first 4 pages
            tile_bytes, page_numbers = create_grid_tile(
                pages,
                tile_number=1,
                grid_size=4,
                add_page_labels=True,
            )
            
            logger.info(f"Created tile for {pdf_path.name} with pages {page_numbers}")
            
            # Always save tile to output directory
            tile_path = self.output_dir / f"{pdf_path.stem}_tile.jpg"
            tile_path.write_bytes(tile_bytes)
            logger.info(f"Saved tile to: {tile_path.name}")
            
            # Build prompt
            prompt = get_sulekha_tile_extraction_prompt(
                tile_number=1,
                page_numbers=page_numbers,
                total_tiles=1,
                total_pages=total_pages,
                is_first_tile=True,
            )
            
            # Extract with LLM
            result = await self.llm_service.chat(
                model=self.model,
                user_prompt=prompt,
                system_prompt=SULEKHA_EXTRACTION_SYSTEM_PROMPT,
                images=[tile_bytes],
                structured_output_schema=SulekhaTileExtraction,
                temperature=0.1,
            )
            
            if not isinstance(result, SulekhaTileExtraction):
                logger.warning(f"Unexpected result type for {pdf_path.name}")
                return None
            
            # Get usage info from the result
            usage_info = getattr(result, '_usage_info', None)
            
            # Convert tile result to full extraction format
            full_result = FullSulekhaExtraction(
                total_pages=len(page_numbers),
                total_projects=len(result.projects),
                document_title=result.document_metadata.get('title') if result.document_metadata else None,
                document_date=result.document_metadata.get('date') if result.document_metadata else None,
                issuing_authority=result.document_metadata.get('issuing_authority') if result.document_metadata else None,
                projects=result.projects,
                extraction_summary=f"Extracted {len(result.projects)} projects from first {len(page_numbers)} pages",
                warnings=[result.notes] if result.notes else [],
                total_cost=sum(p.project_cost for p in result.projects if p.project_cost) or None,
            )
            
            # Convert to dict for JSON serialization
            result_dict = full_result.model_dump()
            result_dict["source_file"] = pdf_path.name
            result_dict["processed_at"] = datetime.now().isoformat()
            
            # Add LLM usage/cost info
            if usage_info:
                result_dict["llm_usage"] = usage_info
            
            # Save individual JSON result
            json_path = self.output_dir / f"{pdf_path.stem}.json"
            with open(json_path, "w", encoding="utf-8") as f:
                json.dump(result_dict, f, indent=2, ensure_ascii=False)
            
            logger.info(f"[{pdf_path.name}] Extracted {full_result.total_projects} projects -> {json_path.name}")
            
            return result_dict
            
        except Exception as e:
            logger.error(f"Failed to process {pdf_path.name}: {str(e)}")
            self.stats["errors"].append({
                "file": pdf_path.name,
                "error": str(e),
            })
            return None
    
    async def run(self) -> Dict[str, Any]:
        """
        Run the batch processing pipeline - all PDFs in parallel.
        
        Returns:
            Combined results dictionary
        """
        self.stats["start_time"] = datetime.now().isoformat()
        
        # Get PDF files
        pdf_files = self.get_pdf_files()
        self.stats["total_pdfs"] = len(pdf_files)
        
        if not pdf_files:
            logger.warning("No PDF files found to process")
            return {"results": [], "stats": self.stats}
        
        logger.info(f"\nProcessing {len(pdf_files)} PDFs in parallel...")
        
        # Process ALL PDFs in parallel
        tasks = [self.process_single_pdf(pdf_path) for pdf_path in pdf_files]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        # Collect successful results
        all_results = []
        total_cost_usd = 0.0
        total_tokens = 0
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                logger.error(f"PDF {pdf_files[i].name} failed: {result}")
                self.stats["failed"] += 1
                self.stats["errors"].append({
                    "file": pdf_files[i].name,
                    "error": str(result),
                })
            elif result is not None:
                all_results.append(result)
                self.stats["successful"] += 1
                self.stats["total_projects"] += result.get("total_projects", 0)
                self.stats["total_pages"] += result.get("total_pages", 0)
                # Track LLM costs
                if "llm_usage" in result:
                    total_cost_usd += result["llm_usage"].get("total_cost_usd", 0)
                    total_tokens += result["llm_usage"].get("total_tokens", 0)
            else:
                self.stats["failed"] += 1
        
        self.stats["total_llm_cost_usd"] = round(total_cost_usd, 6)
        self.stats["total_tokens"] = total_tokens
        
        self.stats["end_time"] = datetime.now().isoformat()
        
        # Create combined output
        combined = {
            "extraction_date": datetime.now().isoformat(),
            "model_used": self.model,
            "stats": self.stats,
            "results": all_results,
        }
        
        # Save combined results
        combined_path = self.output_dir / "results.json"
        with open(combined_path, "w", encoding="utf-8") as f:
            json.dump(combined, f, indent=2, ensure_ascii=False)
        
        logger.info(f"\nCombined results saved to: {combined_path}")
        
        # Print final summary
        self._print_summary()
        
        return combined
    
    def _print_summary(self):
        """Print final processing summary."""
        print("\n" + "=" * 70)
        print("PROCESSING SUMMARY")
        print("=" * 70)
        print(f"Total PDFs processed: {self.stats['total_pdfs']}")
        print(f"Successful: {self.stats['successful']}")
        print(f"Failed: {self.stats['failed']}")
        print(f"Total projects extracted: {self.stats['total_projects']}")
        print(f"Total pages processed: {self.stats['total_pages']}")
        print(f"Total LLM tokens: {self.stats.get('total_tokens', 0):,}")
        print(f"Total LLM cost: ${self.stats.get('total_llm_cost_usd', 0):.6f}")
        
        if self.stats["errors"]:
            print("\nErrors:")
            for err in self.stats["errors"]:
                print(f"  - {err['file']}: {err['error']}")
        
        print(f"\nOutput directory: {self.output_dir}")
        print("=" * 70)


# =============================================================================
# Main Entry Point
# =============================================================================

async def main():
    """Main entry point for the runner."""
    import argparse
    
    parser = argparse.ArgumentParser(
        description="Process Sulekha PDFs and extract municipal project data",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    # Process all PDFs in the default directory
    python runner.py
    
    # Process PDFs from a custom directory
    python runner.py --pdf-dir /path/to/pdfs
    
    # Process a single PDF file
    python runner.py --single /path/to/file.pdf
    
    # Save tile images for debugging
    python runner.py --save-tiles
    
    # Use a different model
    python runner.py --model gemini-2.5-pro

Environment Variables:
    GEMINI_API_KEY    Your Gemini API key (required)
        """
    )
    
    parser.add_argument(
        "--pdf-dir",
        type=Path,
        default=DEFAULT_PDF_DIR,
        help=f"Directory containing PDFs (default: {DEFAULT_PDF_DIR})"
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help=f"Output directory for results (default: {DEFAULT_OUTPUT_DIR})"
    )
    parser.add_argument(
        "--single",
        type=Path,
        help="Process a single PDF file instead of a directory"
    )
    parser.add_argument(
        "--model",
        type=str,
        default=DEFAULT_MODEL,
        choices=["gemini-2.5-flash", "gemini-2.5-pro", "gemini-2.5-flash-lite", "gemini-2.0-flash"],
        help=f"Gemini model to use (default: {DEFAULT_MODEL})"
    )
    parser.add_argument(
        "--save-tiles",
        action="store_true",
        help="Save tile images for debugging"
    )
    parser.add_argument(
        "--api-key",
        type=str,
        default=os.environ.get("GEMINI_API_KEY"),
        help="Gemini API key (default: from GEMINI_API_KEY env var)"
    )
    
    args = parser.parse_args()
    
    # Validate API key
    if not args.api_key:
        print("\n" + "=" * 70)
        print("ERROR: Gemini API key is required!")
        print("=" * 70)
        print("\nYou can provide the API key in one of these ways:")
        print("\n1. Set environment variable:")
        print("   export GEMINI_API_KEY='your-api-key-here'")
        print("\n2. Create a .env file in the project directory with:")
        print("   GEMINI_API_KEY=your-api-key-here")
        print("\n3. Pass it as a command line argument:")
        print("   python runner.py --api-key 'your-api-key-here'")
        print("\nGet your API key from: https://aistudio.google.com/apikey")
        print("=" * 70 + "\n")
        sys.exit(1)
    
    # Handle single file processing
    if args.single:
        if not args.single.exists():
            print(f"Error: File not found: {args.single}")
            sys.exit(1)
        
        # Create a temporary directory with just this file
        runner = SulekhaRunner(
            api_key=args.api_key,
            pdf_dir=args.single.parent,
            output_dir=args.output_dir,
            model=args.model,
            save_tiles=args.save_tiles,
        )
        
        result = await runner.process_single_pdf(args.single)
        if result:
            print(f"\nExtraction successful!")
            print(f"Output saved to: {args.output_dir / args.single.stem}.json")
        else:
            print(f"\nExtraction failed. Check the logs above for details.")
            sys.exit(1)
    else:
        # Batch processing
        runner = SulekhaRunner(
            api_key=args.api_key,
            pdf_dir=args.pdf_dir,
            output_dir=args.output_dir,
            model=args.model,
            save_tiles=args.save_tiles,
        )
        
        await runner.run()


if __name__ == "__main__":
    asyncio.run(main())
