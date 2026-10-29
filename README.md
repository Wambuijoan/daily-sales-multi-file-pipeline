# Soko Fresh Daily Sales Pipeline
 
Soko Fresh has branches in Nairobi, Mombasa, Kisumu and Nakuru. Every night each branch's till system saves the day's sales as a CSV in a shared folder, and finance builds its reports from those files. The files are not always clean: some fields are blank, a retry can re-send transactions that were already sent, and a POS upgrade changed a column name.
 
This script reads every daily file, checks it, and produces three things: one CSV of the good transactions, one CSV of the rejected transactions with the reason for each, and a short summary of the run. 
 
## How to run
 
```bash
pip install -r requirements.txt
python pipeline.py --input data/
```
The output files are overwritten on every run.
 
To add a new day, drop a file named `transactions_YYYY-MM-DD.csv` into `data/`. The script finds files with the pattern `transactions_*.csv`.
 
## Project layout
 
```
multi-file-pipeline/
├── pipeline.py
├── data/
│   ├── transactions_2026-10-01.csv
│   ├── transactions_2026-10-02.csv
│   └── transactions_2026-10-03.csv
├── output/
│   ├── consolidated_transactions.csv
│   ├── rejected_transactions.csv
│   └── summary.txt
├── requirements.txt
└── README.md
```
 
## What it produces
 
- `consolidated_transactions.csv`: every accepted row, with a computed `line_total` (quantity x unit price) and the `source_file` it came from.

- `rejected_transactions.csv`: every rejected row, with its source file, the line number in that file, the transaction ID, all the reasons joined with `;`, and its original columns, so finance can find the row and chase the branch.

- `summary.txt`: files found and processed, rows read, accepted and rejected (with the reasons counted), total revenue in KES and the time the run finished. The same text is printed to the screen.

## How it works
 
1. `discover_files` finds the files with `Path.glob` and sorts them by name. The names contain ISO dates, so name order is date order.
2. `read_transactions` reads a file with the `csv` module into one dictionary per row, and adds the file name and the line number to each row.
3. The header is checked against `REQUIRED_COLUMNS` using set subtraction. Known renamed columns are mapped first (see below).
4. `find_problems` returns every reason a row is invalid. A row can have more than one reason.
5. `clean_row` strips whitespace, converts `quantity` and `unit_price` to integers and adds `line_total`.
6. `write_csv` writes the accepted and rejected rows.
7. `build_summary` formats the summary.

 
## Decisions
 
**The file with the different header.** The 3 October file was exported after the POS upgrade. It uses `qty` instead of `quantity` and has an extra `cashier` column. I map `qty` to `quantity` and process the file normally, because only the name changed and the data is fine. 

The mapping is in `COLUMN_ALIASES`, and the summary lists it under "Files with column issues" so it is visible. `cashier` is not in the consolidated output, but it stays in the rejected file.
 
If a file is still missing required columns after the mapping, the whole file is rejected. Its rows are still counted and written to the rejected file with the reason "file rejected", so they are not lost.
 
**Which copy of a duplicate is kept.** The first accepted copy. Files are read oldest first, then row by row. Later copies are rejected as duplicates, and the reason says which file and line the kept copy came from. 

For example, `TXN-1029` appears in the 2 October file and again in the 3 October file, and only the first is kept. An ID is only remembered once a row is accepted, so if the first copy was rejected for another reason, a later valid copy is still accepted.
 
**What counts as an invalid row.**
- A blank required value. Every required column counts, except that `payment_ref` is not required, since cash sales have none.

- A `quantity` or `unit_price` that is not a whole number, or is zero or negative.

- A transaction ID that was already accepted earlier in the run, in the same file or an earlier one.

- A row with more values than the header has columns.

**Summary counts.** "Rows rejected" is the total and matches the rejected CSV. The lines under it count rows per category, so a row with reasons in two categories is counted in both and the lines can add up to more than the total.
 
## Error handling and logging
 
Progress and problems are reported with the `logging` module: an INFO line per file with its rows read, accepted and rejected, a WARNING when a column is mapped, and an ERROR when a file cannot be read or is rejected. 

If the input folder is missing, has no matching files, or the output cannot be written, the script prints one error message and exits with code 1. A file that cannot be opened or decoded is reported and skipped instead of stopping the run, and its rows are not counted as read.