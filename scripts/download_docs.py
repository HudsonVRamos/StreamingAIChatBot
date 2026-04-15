"""Download AWS documentation PDFs for streaming services.

Usage: python scripts/download_docs.py
Downloads to: docs/ folder
Then upload to S3: aws s3 sync docs/ s3://BUCKET/kb-config/docs/ --region us-east-1
"""

import os
import urllib.request

DOCS_DIR = "docs"
os.makedirs(DOCS_DIR, exist_ok=True)

# AWS documentation PDFs (English — Bedrock understands and responds in PT-BR)
DOCS = {
    # User Guides
    "medialive-user-guide.pdf": "https://docs.aws.amazon.com/medialive/latest/ug/medialive-ug.pdf",
    "mediapackage-user-guide.pdf": "https://docs.aws.amazon.com/mediapackage/latest/ug/mediapackage-ug.pdf",
    "mediatailor-user-guide.pdf": "https://docs.aws.amazon.com/mediatailor/latest/ug/mediatailor-ug.pdf",
    "cloudfront-developer-guide.pdf": "https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/AmazonCloudFront_DevGuide.pdf",

    # API References
    "medialive-api-reference.pdf": "https://docs.aws.amazon.com/medialive/latest/apireference/medialive-api.pdf",
    "mediatailor-api-reference.pdf": "https://docs.aws.amazon.com/mediatailor/latest/apireference/mediatailor-api.pdf",
    "cloudfront-api-reference.pdf": "https://docs.aws.amazon.com/cloudfront/latest/APIReference/cf-api.pdf",
}

for filename, url in DOCS.items():
    filepath = os.path.join(DOCS_DIR, filename)
    if os.path.exists(filepath):
        print(f"  Skipping {filename} (already exists)")
        continue
    print(f"  Downloading {filename}...")
    try:
        urllib.request.urlretrieve(url, filepath)
        size_mb = os.path.getsize(filepath) / (1024 * 1024)
        print(f"  OK ({size_mb:.1f} MB)")
    except Exception as e:
        print(f"  FAILED: {e}")

print(f"\nDone. Upload to S3 with:")
print(f"  aws s3 sync docs/ s3://YOUR_KB_CONFIG_BUCKET/kb-config/docs/ --region us-east-1")
