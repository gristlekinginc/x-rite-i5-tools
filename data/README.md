# Sample readings

`sample-readings.csv` — real Color i5 measurements with full 360–750 nm spectra
(10 nm steps), in the same column format the CLI and GUI write. Included so you can try
the GUI with no instrument attached:

```bash
cd ../gui && python app.py --load ../data/sample-readings.csv
```

| label | what it is |
|---|---|
| `green-coffee-ground-sample-01-test` | Green (unroasted) coffee, ground — L\* 59.76, the light end of the roast axis. |
| `joshua-tree-dark-roast-001/-002/-003` | A dark, oily roast read three times without repacking — L\* 32.5 / 31.9 / 32.6 (0.7 spread). Note the collapsed chroma (b\* ~3): the spectral signature of oily dark roast. |

All were measured in **SCE** mode through the flat bottom of a glass dish (ground coffee,
depth-stop tamped, cloth shroud over the dish).
