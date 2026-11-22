#!/usr/bin/env bash
source /opt/ocr/mineru/bin/activate
mineru-kit --help
echo ======; mineru-kit parse --help
echo ======; mineru-kit models --help
echo ======; mineru-kit models download --help
echo ======; mineru-kit config --help 2>&1 | head -40
echo ======; ls /opt/ocr/mineru/bin | grep -i mineru
echo ======; python -c "import mineru, os; print(os.path.dirname(mineru.__file__))"; ls $(python -c "import mineru, os; print(os.path.dirname(mineru.__file__))")
