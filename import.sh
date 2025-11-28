#!/bin/bash

# Set default safety level from command line argument or use "safe" as fallback
DEFAULT_SAFETY="safe"
if [[ $# -ge 1 ]]; then
    case "${1,,}" in
        safe|sketchy|unsafe)
            DEFAULT_SAFETY="${1,,}"
            ;;
        *)
            echo "Warning: Invalid safety level '$1'. Using default 'safe'."
            echo "Valid options: safe, sketchy, unsafe"
            ;;
    esac
fi

while true; do
  read -p "Enter URL(s):" input_url

  if [[ -z "$input_url" ]]; then
    echo "URL cannot be empty."
    continue
  fi

  read -p "Safety level [safe|sketchy|unsafe] (default: $DEFAULT_SAFETY): " safety_level

  # Set default safety level if empty
  if [[ -z "$safety_level" ]]; then
      safety_level="$DEFAULT_SAFETY"
      echo "Using default safety level: $safety_level"
  else
      # Validate safety level input
      if [[ ! "$safety_level" =~ ^(safe|sketchy|unsafe)$ ]]; then
          echo "Warning: '$safety_level' is not a valid safety level. Using 'safe' instead."
          safety_level="safe"
      fi
  fi
  read -p "Are there potential related pictures? [y/N] (default: N): " potential_rels

  # Set tags based on potential_rels response
  if [[ "$potential_rels" =~ ^[Yy]$ ]]; then
      tags="tagme,potential_rels"
      echo "Using tags: tagme,potential_rels"
  else
      tags="tagme"
      echo "Using tags: tagme"
  fi
  uv run szuru-toolkit --log-level DEBUG import-from-url --default-safety "$safety_level" --add-tags "$tags" "$input_url"
done
