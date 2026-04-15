#!/bin/bash

OFFICE_APPS=(
"/Applications/Microsoft Word.app"
"/Applications/Microsoft Excel.app"
"/Applications/Microsoft PowerPoint.app"
"/Applications/Microsoft Outlook.app"
)

OFFICE_FOUND=false

for app in "${OFFICE_APPS[@]}"; do
  if [ -d "$app" ]; then
    OFFICE_FOUND=true
    echo "Office 365 app found: $(basename "$app")"
  fi
done

if [ "$OFFICE_FOUND" = false ]; then
  echo "No Office 365 apps detected on this Mac."
fi

