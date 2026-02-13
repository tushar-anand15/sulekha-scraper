"""
PDF Utility Functions for Sulekha Project Extraction.

Ported from EduCorrect - handles:
- PDF to image conversion
- 2x2 grid tile creation for efficient VLM processing
- Page number overlays for visual reference
"""

import io
import logging
from pathlib import Path
from typing import List, Optional, Tuple

from PIL import Image

logger = logging.getLogger(__name__)


def get_processing_config() -> dict:
    """Get processing configuration."""
    return {
        'grid_size': 4,           # Pages per tile (2x2)
        'dpi': 150,               # Resolution for PDF conversion
        'jpeg_quality': 85,       # Tile image quality
        'max_pages_per_pdf': 100, # Safety limit
    }


def convert_pdf_to_images(
    pdf_bytes: bytes,
    dpi: Optional[int] = None,
) -> List[Image.Image]:
    """
    Convert PDF to list of PIL Images.
    
    Args:
        pdf_bytes: Raw PDF file bytes
        dpi: Resolution for conversion (default from config)
    
    Returns:
        List of PIL Image objects, one per page
    """
    try:
        from pdf2image import convert_from_bytes
    except ImportError:
        raise RuntimeError(
            "pdf2image is not installed. Install with: pip install pdf2image\n"
            "Also requires poppler: brew install poppler (macOS) or apt install poppler-utils (Linux)"
        )
    
    config = get_processing_config()
    dpi = dpi or config.get('dpi', 150)
    max_pages = config.get('max_pages_per_pdf', 100)
    
    logger.info(f"[PDF_UTILS] Converting PDF to images at {dpi} DPI")
    
    try:
        pages = convert_from_bytes(
            pdf_bytes,
            dpi=dpi,
            fmt='RGB',
        )
        
        if len(pages) > max_pages:
            logger.warning(
                f"[PDF_UTILS] PDF has {len(pages)} pages, exceeding limit of {max_pages}. "
                f"Truncating to first {max_pages} pages."
            )
            pages = pages[:max_pages]
        
        logger.info(f"[PDF_UTILS] Converted {len(pages)} pages")
        return pages
        
    except Exception as e:
        logger.error(f"[PDF_UTILS] Error converting PDF: {str(e)}")
        raise


def add_page_number_overlay(
    image: Image.Image,
    page_number: int,
    position: str = "top-left",
) -> Image.Image:
    """
    Add a page number overlay to an image.
    
    Args:
        image: PIL Image to annotate
        page_number: Page number to display
        position: Position hint (used to offset label from edge)
    
    Returns:
        Annotated PIL Image
    """
    from PIL import ImageDraw, ImageFont
    
    # Create a copy to avoid modifying original
    img = image.copy()
    draw = ImageDraw.Draw(img)
    
    # Calculate font size based on image dimensions (roughly 3% of width)
    font_size = max(24, int(img.width * 0.03))
    
    # Try to use a nice font, fall back to default
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", font_size)
    except (OSError, IOError):
        try:
            font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", font_size)
        except (OSError, IOError):
            font = ImageFont.load_default()
    
    label = f"Page {page_number}"
    
    # Get text bounding box
    bbox = draw.textbbox((0, 0), label, font=font)
    text_width = bbox[2] - bbox[0]
    text_height = bbox[3] - bbox[1]
    
    # Add padding
    padding = 10
    box_width = text_width + padding * 2
    box_height = text_height + padding * 2
    
    # Position in top-left corner with margin
    margin = 15
    x = margin
    y = margin
    
    # Draw semi-transparent background box
    overlay = Image.new('RGBA', img.size, (0, 0, 0, 0))
    overlay_draw = ImageDraw.Draw(overlay)
    overlay_draw.rectangle(
        [x, y, x + box_width, y + box_height],
        fill=(0, 0, 0, 180),  # Semi-transparent black
        outline=(255, 255, 255, 255),
        width=2
    )
    
    # Composite overlay onto image
    if img.mode != 'RGBA':
        img = img.convert('RGBA')
    img = Image.alpha_composite(img, overlay)
    
    # Draw text
    draw = ImageDraw.Draw(img)
    draw.text(
        (x + padding, y + padding),
        label,
        font=font,
        fill=(255, 255, 255, 255)  # White text
    )
    
    # Convert back to RGB for JPEG saving
    return img.convert('RGB')


def create_grid_tile(
    pages: List[Image.Image],
    tile_number: int,
    grid_size: int = 4,
    jpeg_quality: Optional[int] = None,
    add_page_labels: bool = True,
) -> Tuple[bytes, List[int]]:
    """
    Create a 2x2 grid tile from up to 4 pages.
    
    Args:
        pages: List of all page images
        tile_number: Which tile to create (1-indexed)
        grid_size: Number of pages per tile (default 4 for 2x2)
        jpeg_quality: JPEG quality (default from config)
        add_page_labels: Whether to add page number labels to each quadrant
    
    Returns:
        Tuple of (grid_image_bytes, page_numbers_in_tile)
    """
    config = get_processing_config()
    jpeg_quality = jpeg_quality or config.get('jpeg_quality', 85)
    
    # Calculate which pages go in this tile
    start_idx = (tile_number - 1) * grid_size
    end_idx = min(start_idx + grid_size, len(pages))
    tile_pages = pages[start_idx:end_idx]
    page_numbers = list(range(start_idx + 1, end_idx + 1))  # 1-indexed
    
    if not tile_pages:
        raise ValueError(f"No pages for tile {tile_number}")
    
    logger.info(f"[PDF_UTILS] Creating tile {tile_number} with pages {page_numbers}")
    
    # Get dimensions from first page
    page_width, page_height = tile_pages[0].size
    
    # Create 2x2 grid canvas
    grid_width = page_width * 2
    grid_height = page_height * 2
    grid_image = Image.new('RGB', (grid_width, grid_height), 'white')
    
    # Position pages in 2x2 layout
    positions = [
        (0, 0),                     # Top-left
        (page_width, 0),            # Top-right
        (0, page_height),           # Bottom-left
        (page_width, page_height),  # Bottom-right
    ]
    
    for i, page in enumerate(tile_pages):
        if i < len(positions):
            # Resize page to fit if needed (maintain aspect ratio)
            page_resized = page.resize(
                (page_width, page_height),
                Image.Resampling.LANCZOS
            )
            
            # Add page number label if enabled
            if add_page_labels:
                page_resized = add_page_number_overlay(
                    page_resized, 
                    page_numbers[i]
                )
            
            grid_image.paste(page_resized, positions[i])
    
    # Convert to bytes
    buffer = io.BytesIO()
    grid_image.save(buffer, format='JPEG', quality=jpeg_quality)
    grid_bytes = buffer.getvalue()
    
    logger.info(
        f"[PDF_UTILS] Created tile {tile_number}: {len(grid_bytes)} bytes, "
        f"{grid_width}x{grid_height}px, with_labels={add_page_labels}"
    )
    
    return grid_bytes, page_numbers


def create_all_grid_tiles(
    pdf_bytes: bytes,
    dpi: Optional[int] = None,
    grid_size: int = 4,
    add_page_labels: bool = True,
) -> List[Tuple[bytes, List[int]]]:
    """
    Create all grid tiles from a PDF.
    
    Args:
        pdf_bytes: Raw PDF file bytes
        dpi: Resolution for conversion
        grid_size: Number of pages per tile (default 4)
        add_page_labels: Whether to add page number labels
    
    Returns:
        List of (grid_image_bytes, page_numbers) tuples
    """
    pages = convert_pdf_to_images(pdf_bytes, dpi=dpi)
    
    if not pages:
        logger.warning("[PDF_UTILS] No pages in PDF")
        return []
    
    # Calculate number of tiles needed
    num_tiles = (len(pages) + grid_size - 1) // grid_size
    
    logger.info(f"[PDF_UTILS] Creating {num_tiles} tiles from {len(pages)} pages")
    
    tiles = []
    for tile_num in range(1, num_tiles + 1):
        try:
            tile_bytes, page_nums = create_grid_tile(
                pages,
                tile_number=tile_num,
                grid_size=grid_size,
                add_page_labels=add_page_labels,
            )
            tiles.append((tile_bytes, page_nums))
        except Exception as e:
            logger.error(f"[PDF_UTILS] Error creating tile {tile_num}: {str(e)}")
            raise
    
    logger.info(f"[PDF_UTILS] Created {len(tiles)} tiles successfully")
    return tiles


def create_grid_tile_from_page_images(
    page_images: List[Image.Image],
    page_numbers: List[int],
    jpeg_quality: Optional[int] = None,
    add_page_labels: bool = True,
) -> bytes:
    """
    Create a 2x2 grid tile from specific page images.
    
    Unlike create_grid_tile which works with sequential tile numbers,
    this function creates a tile from arbitrary specific pages.
    
    Args:
        page_images: List of all page images (0-indexed)
        page_numbers: Specific page numbers to include (1-indexed)
        jpeg_quality: JPEG quality (default from config)
        add_page_labels: Whether to add page number labels
    
    Returns:
        Grid image as bytes
    """
    config = get_processing_config()
    jpeg_quality = jpeg_quality or config.get('jpeg_quality', 85)
    
    # Get the pages we need (convert 1-indexed to 0-indexed)
    selected_pages = []
    valid_page_numbers = []
    for page_num in page_numbers:
        if 0 < page_num <= len(page_images):
            selected_pages.append(page_images[page_num - 1])
            valid_page_numbers.append(page_num)
    
    if not selected_pages:
        raise ValueError(f"No valid pages for numbers {page_numbers}")
    
    logger.info(f"[PDF_UTILS] Creating grid tile from pages {valid_page_numbers}")
    
    # Get dimensions from first page
    page_width, page_height = selected_pages[0].size
    
    # Create 2x2 grid canvas
    grid_width = page_width * 2
    grid_height = page_height * 2
    grid_image = Image.new('RGB', (grid_width, grid_height), 'white')
    
    # Position pages in 2x2 layout
    positions = [
        (0, 0),                     # Top-left
        (page_width, 0),            # Top-right
        (0, page_height),           # Bottom-left
        (page_width, page_height),  # Bottom-right
    ]
    
    for i, page in enumerate(selected_pages):
        if i < len(positions):
            page_resized = page.resize(
                (page_width, page_height),
                Image.Resampling.LANCZOS
            )
            
            # Add page number label if enabled
            if add_page_labels and i < len(valid_page_numbers):
                page_resized = add_page_number_overlay(
                    page_resized,
                    valid_page_numbers[i]
                )
            
            grid_image.paste(page_resized, positions[i])
    
    # Convert to bytes
    buffer = io.BytesIO()
    grid_image.save(buffer, format='JPEG', quality=jpeg_quality)
    return buffer.getvalue()


def get_page_count(pdf_bytes: bytes) -> int:
    """
    Get the number of pages in a PDF without full conversion.
    
    Args:
        pdf_bytes: Raw PDF file bytes
    
    Returns:
        Number of pages
    """
    try:
        from pdf2image import pdfinfo_from_bytes
        info = pdfinfo_from_bytes(pdf_bytes)
        return info.get('Pages', 0)
    except ImportError:
        # Fallback: do full conversion
        logger.warning("[PDF_UTILS] pdfinfo not available, doing full conversion")
        pages = convert_pdf_to_images(pdf_bytes)
        return len(pages)
    except Exception as e:
        logger.error(f"[PDF_UTILS] Error getting page count: {str(e)}")
        return 0


def extract_single_page(
    pdf_bytes: bytes,
    page_number: int,
    dpi: Optional[int] = None,
) -> Optional[Image.Image]:
    """
    Extract a single page from a PDF.
    
    Args:
        pdf_bytes: Raw PDF file bytes
        page_number: Page number to extract (1-indexed)
        dpi: Resolution for conversion
    
    Returns:
        PIL Image of the page, or None if not found
    """
    try:
        from pdf2image import convert_from_bytes
        
        config = get_processing_config()
        dpi = dpi or config.get('dpi', 150)
        
        pages = convert_from_bytes(
            pdf_bytes,
            dpi=dpi,
            first_page=page_number,
            last_page=page_number,
            fmt='RGB',
        )
        
        if pages:
            return pages[0]
        return None
        
    except Exception as e:
        logger.error(f"[PDF_UTILS] Error extracting page {page_number}: {str(e)}")
        return None


# Convenience function for saving tiles to disk (useful for debugging)
def save_tile_to_file(
    tile_bytes: bytes,
    output_path: Path,
) -> None:
    """Save tile bytes to a file for debugging."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(tile_bytes)
    logger.info(f"[PDF_UTILS] Saved tile to {output_path}")
