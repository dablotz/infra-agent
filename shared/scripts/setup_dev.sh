#!/bin/bash
set -e

echo "Setting up local development environment..."

if ! command -v python3 &> /dev/null; then
    echo "Error: Python 3 is required but not installed"
    exit 1
fi

if ! command -v terraform &> /dev/null; then
    echo "Warning: Terraform not found. Install from https://www.terraform.io/downloads"
fi

if ! command -v aws &> /dev/null; then
    echo "Warning: AWS CLI not found. Install from https://aws.amazon.com/cli/"
fi

echo "Creating Python virtual environment..."
python3 -m venv venv

echo "Activating virtual environment..."
source venv/bin/activate

echo "Installing Python dependencies..."
pip install --upgrade pip
pip install -r requirements.txt

echo "Installing pre-commit hooks..."
pre-commit install

echo ""
echo "Setup complete!"
echo ""
echo "To activate the virtual environment, run:"
echo "  source venv/bin/activate"
