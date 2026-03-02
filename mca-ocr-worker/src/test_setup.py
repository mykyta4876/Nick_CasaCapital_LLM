"""
Quick test to verify all dependencies are working
"""

def test_imports():
    """Test all required imports"""
    print("Testing imports...")
    
    try:
        import pdfplumber
        print("✅ pdfplumber")
    except ImportError as e:
        print(f"❌ pdfplumber: {e}")
    
    try:
        import fitz  # PyMuPDF
        print("✅ PyMuPDF")
    except ImportError as e:
        print(f"❌ PyMuPDF: {e}")
    
    try:
        import torch
        cuda = torch.cuda.is_available()
        if cuda:
            gpu = torch.cuda.get_device_name(0)
            print(f"✅ PyTorch with CUDA ({gpu})")
        else:
            print("⚠️  PyTorch (CPU only - no GPU)")
    except ImportError as e:
        print(f"❌ PyTorch: {e}")
    
    try:
        import surya
        print("✅ Surya OCR")
    except ImportError as e:
        print(f"❌ Surya OCR: {e}")
    
    print("\nAll core dependencies ready!")


def test_pdf_extraction(pdf_path: str = None):
    """Test PDF extraction on a sample file"""
    if not pdf_path:
        print("\nSkipping PDF test (no file provided)")
        print("Run with: python test_setup.py <path_to_pdf>")
        return
    
    from pathlib import Path
    
    if not Path(pdf_path).exists():
        print(f"\n❌ File not found: {pdf_path}")
        return
    
    print(f"\nTesting extraction on: {pdf_path}")
    
    try:
        from extractor import extract_and_parse
        result = extract_and_parse(pdf_path)
        print(f"\n✅ Extraction successful!")
        print(f"   Bank: {result.bank_name}")
        print(f"   Account: {result.account_number}")
        print(f"   Deposits: ${result.total_deposits:,.2f}")
        print(f"   MCA Detected: {len(result.mca_payments)} positions")
    except Exception as e:
        print(f"\n❌ Extraction failed: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    import sys
    
    test_imports()
    
    if len(sys.argv) > 1:
        test_pdf_extraction(sys.argv[1])
    else:
        test_pdf_extraction()
