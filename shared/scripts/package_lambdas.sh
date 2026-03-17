#!/bin/bash
set -e

echo "Packaging Lambda functions..."

for dir in lambda_functions/*/; do
    func_name=$(basename "$dir")
    echo "Packaging $func_name..."
    cd "$dir"
    zip -q "../${func_name}.zip" handler.py
    cd - > /dev/null
done

echo "Lambda functions packaged successfully"
echo ""
echo "Note: Lambda layers for terraform_tools and security_tools need to be built separately:"
echo "  - terraform_tools.zip should contain terraform and tflint binaries in bin/"
echo "  - security_tools.zip should contain checkov Python package"
