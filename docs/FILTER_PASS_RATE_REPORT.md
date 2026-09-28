# Filter pass-rate report (cached lists)

Generated: **2026-07-29 13:58:34** (UTC).

Read-only calibration: stocks loaded via `fetch_business_list_stocks(..., force_yf=False)`.
No filter defaults were changed. Gates mirror `templates/index.html` defaults and `data/pariah_filter.py`.

## Defaults used

| Screen | Key defaults |
|--------|--------------|
| **Burry** | max 52w gap 15%; require positive FCF; cheap if P/FCF≤15 or FCF yield≥5% or EV/FCF≤12; D/E≤0.5 or net cash or debt/mcap≤0.35; EV/EBITDA ≤ sector median (min 5 peers) else list median |
| **Templeton** | max 52w gap 25%; D/E≤1.0; net D/E≤0.8; debt/EBITDA≤3 (est.); P/B≤1.5; EV/EBITDA ≤ peer median; require leverage data |
| **Pariah** | industry/keyword membership; depressed (52w gap≤30% **or** 200w MA≤−20%); clean BS (D/E≤0.25 or net cash; hard D/E 1.0); cheap vs capital (P/B≤1.2 or net-assets vs mcap) |

**Pass rate (complete)** = passers / non-pending rows. **Pass rate (all)** = passers / full list (pending count as fail).

## Summary table

| List | N | Pending | Burry % (complete) | Templeton % | Pariah % | List EV/EBITDA med |
|------|---:|--------:|-------------------:|------------:|---------:|-------------------:|
| ASX Listed Companies | 1764 | 0 | 2.72% (48/1764) | 4.59% (81/1764) | 14.34% (253/1764) | 10.9 |
| US S&P 500 | 503 | 0 | 3.98% (20/503) | 1.99% (10/503) | 0.00% (0/503) | 15.4 |
| US Russell 1000 | 1013 | 0 | 3.06% (31/1013) | 1.88% (19/1013) | 0.10% (1/1013) | 14.8 |
| US Nasdaq-100 | 103 | 0 | 2.91% (3/103) | 0.00% (0/103) | 0.00% (0/103) | 25.3 |
| Asia Emerging businesses | 1088 | 0 | 8.92% (97/1088) | 10.85% (118/1088) | 4.87% (53/1088) | 7.8 |
| Australasian business url list | 1316 | 0 | 3.04% (40/1316) | 4.86% (64/1316) | 14.13% (186/1316) | 10.9 |
| Energy businesses list | 156 | 0 | 1.92% (3/156) | 5.77% (9/156) | 7.05% (11/156) | 8.7 |
| japan stocks | 3548 | 0 | 6.34% (225/3548) | 18.66% (662/3548) | 2.37% (84/3548) | 7.2 |
| korea stocks | 2682 | 2000 | 10.12% (69/682) | 0.00% (0/682) | 8.36% (57/682) | 6.4 |
| Large core businesses list | 91 | 0 | 2.20% (2/91) | 0.00% (0/91) | 0.00% (0/91) | 16.1 |
| Large growth businesses list | 64 | 0 | 6.25% (4/64) | 0.00% (0/64) | 0.00% (0/64) | 20.6 |
| Large value businesses list | 53 | 0 | 1.89% (1/53) | 1.89% (1/53) | 0.00% (0/53) | 11.9 |
| Materials businesses list | 121 | 0 | 0.00% (0/121) | 3.31% (4/121) | 4.13% (5/121) | 11.3 |
| Mid-cap core businesses list | 180 | 0 | 3.89% (7/180) | 2.22% (4/180) | 0.00% (0/180) | 14.7 |
| Mid-cap growth business url list | 125 | 0 | 2.40% (3/125) | 0.80% (1/125) | 0.00% (0/125) | 20.3 |
| Mid-cap value business url list | 135 | 0 | 2.22% (3/135) | 4.44% (6/135) | 0.00% (0/135) | 10.2 |
| Small cap core business url list | 2171 | 0 | 4.01% (87/2171) | 5.67% (123/2171) | 4.56% (99/2171) | 10.8 |
| Small cap growth business url list | 469 | 0 | 1.49% (7/469) | 2.35% (11/469) | 0.64% (3/469) | 17.0 |
| Small cap value business url list | 1102 | 0 | 1.63% (18/1102) | 4.08% (45/1102) | 0.73% (8/1102) | 10.3 |

## Per-list detail

### ASX Listed Companies

- `list_id`: `__builtin__:asx`
- Universe loaded: **1764** (pending YF: **0**)
- Peer EV/EBITDA: median **10.9** over 611 names with positive multiple

#### Burry

- Pass: **48** / 1764 complete (**2.72%**); all-list rate 2.72% including 0 incomplete
- Gate pass rates (among complete):

| Gate | Pass % | Fail count |
|------|-------:|-----------:|
| `nearLow` | 39.3% | 1071 |
| `fcf` | 35.9% | 1131 |
| `cheap` | 21.8% | 1379 |
| `debt` | 83.7% | 288 |
| `evEbitda` | 17.6% | 1454 |

- Top fail gate combinations (failed gates only):

  - `nearLow,fcf,cheap,evEbitda`: 583
  - `fcf,cheap,evEbitda`: 354
  - `nearLow,cheap,evEbitda`: 119
  - `nearLow,evEbitda`: 84
  - `nearLow`: 81
  - `fcf,cheap,debt,evEbitda`: 69
  - `nearLow,fcf,cheap,debt,evEbitda`: 59
  - `evEbitda`: 57

- Sample passers: HSN.AX, GLB.AX, ECH.AX, GDF.AX, CAA.AX, TPG.AX, BGP.AX, GQG.AX, SFC.AX, MAM.AX, SPZ.AX, DXS.AX, CHL.AX, COS.AX, MGR.AX

#### Templeton

- Pass: **81** / 1764 complete (**4.59%**); all-list rate 4.59% including 0 incomplete
- Gate pass rates (among complete):

| Gate | Pass % | Fail count |
|------|-------:|-----------:|
| `nearLow` | 52.7% | 835 |
| `de` | 86.4% | 240 |
| `netDe` | 91.6% | 149 |
| `debtEbitda` | 72.6% | 483 |
| `evEbitda` | 17.6% | 1454 |
| `pb` | 43.2% | 1002 |

- Top fail gate combinations (failed gates only):

  - `nearLow,evEbitda,pb`: 399
  - `evEbitda,pb`: 254
  - `evEbitda`: 247
  - `nearLow,evEbitda`: 157
  - `debtEbitda,evEbitda`: 86
  - `pb`: 46
  - `nearLow,de,debtEbitda,evEbitda,pb`: 46
  - `nearLow,debtEbitda,evEbitda,pb`: 45

- Sample passers: SSL.AX, KSC.AX, GLB.AX, FFI.AX, ECH.AX, CAA.AX, ASB.AX, ZIM.AX, CHL.AX, CIA.AX, FRI.AX, EPI.AX, MRT.AX, SRT.AX, ANG.AX

#### Pariah

- Pass: **253** / 1764 complete (**14.34%**); all-list rate 14.34% including 0 incomplete
- Gate pass rates (among complete):

| Gate | Pass % | Fail count |
|------|-------:|-----------:|
| `industry` | 48.2% | 914 |
| `depressed_price` | 70.4% | 522 |
| `clean_bs` | 71.9% | 495 |
| `cheap_vs_capital` | 43.9% | 989 |

- Top fail gate combinations (failed gates only):

  - `depressed_price,cheap_vs_capital`: 240
  - `industry`: 213
  - `cheap_vs_capital`: 211
  - `industry,cheap_vs_capital`: 182
  - `industry,clean_bs`: 179
  - `industry,clean_bs,cheap_vs_capital`: 132
  - `industry,depressed_price,cheap_vs_capital`: 111
  - `industry,depressed_price,clean_bs,cheap_vs_capital`: 53

- Sample passers: ECH.AX, JLL.AX, VUL.AX, FAR.AX, ZIM.AX, PAF.AX, TET.AX, ATM.AX, MRT.AX, SRT.AX, AVL.AX, HAS.AX, MBG.AX, AEL.AX, EGA.AX

### US S&P 500

- `list_id`: `__builtin__:sp500`
- Universe loaded: **503** (pending YF: **0**)
- Peer EV/EBITDA: median **15.4** over 470 names with positive multiple

#### Burry

- Pass: **20** / 503 complete (**3.98%**); all-list rate 3.98% including 0 incomplete
- Gate pass rates (among complete):

| Gate | Pass % | Fail count |
|------|-------:|-----------:|
| `nearLow` | 22.7% | 389 |
| `fcf` | 77.5% | 113 |
| `cheap` | 28.6% | 359 |
| `debt` | 64.8% | 177 |
| `evEbitda` | 47.5% | 264 |

- Top fail gate combinations (failed gates only):

  - `nearLow,cheap,evEbitda`: 112
  - `nearLow`: 43
  - `nearLow,fcf,cheap,evEbitda`: 38
  - `nearLow,cheap`: 38
  - `nearLow,debt`: 37
  - `nearLow,fcf,cheap,debt,evEbitda`: 30
  - `nearLow,cheap,debt,evEbitda`: 26
  - `cheap,evEbitda`: 23

- Sample passers: TTD, OTIS, LEN, EXE, RMD, PNR, PTC, LULU, EQT, AIG, BR, DHI, CRM, HII, AOS

#### Templeton

- Pass: **10** / 503 complete (**1.99%**); all-list rate 1.99% including 0 incomplete
- Gate pass rates (among complete):

| Gate | Pass % | Fail count |
|------|-------:|-----------:|
| `nearLow` | 44.9% | 277 |
| `de` | 58.4% | 209 |
| `netDe` | 67.0% | 166 |
| `debtEbitda` | 58.1% | 211 |
| `evEbitda` | 47.5% | 264 |
| `pb` | 8.5% | 460 |

- Top fail gate combinations (failed gates only):

  - `nearLow,evEbitda,pb`: 73
  - `pb`: 47
  - `nearLow,pb`: 45
  - `evEbitda,pb`: 35
  - `de,netDe,debtEbitda,pb`: 30
  - `nearLow,de,netDe,debtEbitda,evEbitda,pb`: 24
  - `nearLow,de,netDe,debtEbitda,pb`: 23
  - `nearLow,debtEbitda,evEbitda,pb`: 20

- Sample passers: LEN, TAP, EXE, EQT, AIG, UHS, MOS, TSN, ZBH, CTSH

#### Pariah

- Pass: **0** / 503 complete (**0.00%**); all-list rate 0.00% including 0 incomplete
- Gate pass rates (among complete):

| Gate | Pass % | Fail count |
|------|-------:|-----------:|
| `industry` | 11.5% | 445 |
| `depressed_price` | 54.7% | 228 |
| `clean_bs` | 22.5% | 390 |
| `cheap_vs_capital` | 10.9% | 448 |

- Top fail gate combinations (failed gates only):

  - `industry,clean_bs,cheap_vs_capital`: 159
  - `industry,depressed_price,clean_bs,cheap_vs_capital`: 134
  - `industry,cheap_vs_capital`: 51
  - `industry,depressed_price,cheap_vs_capital`: 48
  - `industry,clean_bs`: 31
  - `clean_bs,cheap_vs_capital`: 26
  - `depressed_price,clean_bs,cheap_vs_capital`: 22
  - `industry,depressed_price,clean_bs`: 16

- Sample passers: *(none)*

### US Russell 1000

- `list_id`: `__builtin__:russell1000`
- Universe loaded: **1013** (pending YF: **0**)
- Peer EV/EBITDA: median **14.8** over 883 names with positive multiple

#### Burry

- Pass: **31** / 1013 complete (**3.06%**); all-list rate 3.06% including 0 incomplete
- Gate pass rates (among complete):

| Gate | Pass % | Fail count |
|------|-------:|-----------:|
| `nearLow` | 20.9% | 801 |
| `fcf` | 75.3% | 250 |
| `cheap` | 31.1% | 698 |
| `debt` | 63.0% | 375 |
| `evEbitda` | 44.0% | 567 |

- Top fail gate combinations (failed gates only):

  - `nearLow,cheap,evEbitda`: 214
  - `nearLow,debt`: 101
  - `nearLow,fcf,cheap,evEbitda`: 91
  - `nearLow`: 80
  - `nearLow,cheap`: 72
  - `nearLow,fcf,cheap,debt,evEbitda`: 60
  - `nearLow,cheap,debt,evEbitda`: 54
  - `debt`: 38

- Sample passers: CNM, DLB, TTD, OTIS, LOPE, DOX, LEN, LEN-B, PEGA, INGR, EXE, RMD, PNR, PTC, LULU

#### Templeton

- Pass: **19** / 1013 complete (**1.88%**); all-list rate 1.88% including 0 incomplete
- Gate pass rates (among complete):

| Gate | Pass % | Fail count |
|------|-------:|-----------:|
| `nearLow` | 40.5% | 603 |
| `de` | 59.4% | 411 |
| `netDe` | 66.5% | 339 |
| `debtEbitda` | 52.5% | 481 |
| `evEbitda` | 44.0% | 567 |
| `pb` | 14.1% | 870 |

- Top fail gate combinations (failed gates only):

  - `nearLow,evEbitda,pb`: 151
  - `nearLow,pb`: 88
  - `pb`: 75
  - `evEbitda,pb`: 60
  - `nearLow,de,netDe,debtEbitda,evEbitda,pb`: 60
  - `de,netDe,debtEbitda,pb`: 52
  - `nearLow,de,netDe,debtEbitda,pb`: 42
  - `nearLow,debtEbitda,evEbitda,pb`: 41

- Sample passers: LEN, INGR, TAP, EXE, THO, EQT, AIG, UHS, MOS, GLOB, TSN, GLIBK, ZBH, MKL, MTG

#### Pariah

- Pass: **1** / 1013 complete (**0.10%**); all-list rate 0.10% including 0 incomplete
- Gate pass rates (among complete):

| Gate | Pass % | Fail count |
|------|-------:|-----------:|
| `industry` | 11.1% | 901 |
| `depressed_price` | 52.5% | 481 |
| `clean_bs` | 26.8% | 742 |
| `cheap_vs_capital` | 14.0% | 871 |

- Top fail gate combinations (failed gates only):

  - `industry,clean_bs,cheap_vs_capital`: 284
  - `industry,depressed_price,clean_bs,cheap_vs_capital`: 256
  - `industry,depressed_price,cheap_vs_capital`: 116
  - `industry,cheap_vs_capital`: 114
  - `industry,clean_bs`: 69
  - `industry,depressed_price,clean_bs`: 43
  - `depressed_price,clean_bs,cheap_vs_capital`: 43
  - `clean_bs,cheap_vs_capital`: 38

- Sample passers: XE

### US Nasdaq-100

- `list_id`: `__builtin__:nasdaq100`
- Universe loaded: **103** (pending YF: **0**)
- Peer EV/EBITDA: median **25.3** over 100 names with positive multiple

#### Burry

- Pass: **3** / 103 complete (**2.91%**); all-list rate 2.91% including 0 incomplete
- Gate pass rates (among complete):

| Gate | Pass % | Fail count |
|------|-------:|-----------:|
| `nearLow` | 23.3% | 79 |
| `fcf` | 85.4% | 15 |
| `cheap` | 15.5% | 87 |
| `debt` | 85.4% | 15 |
| `evEbitda` | 53.4% | 48 |

- Top fail gate combinations (failed gates only):

  - `nearLow,cheap,evEbitda`: 33
  - `nearLow,cheap`: 22
  - `nearLow`: 9
  - `cheap,evEbitda`: 7
  - `cheap`: 5
  - `nearLow,fcf,cheap,debt`: 4
  - `debt`: 3
  - `nearLow,fcf,cheap`: 3

- Sample passers: NFLX, ADSK, PDD

#### Templeton

- Pass: **0** / 103 complete (**0.00%**); all-list rate 0.00% including 0 incomplete
- Gate pass rates (among complete):

| Gate | Pass % | Fail count |
|------|-------:|-----------:|
| `nearLow` | 42.7% | 59 |
| `de` | 74.8% | 26 |
| `netDe` | 82.5% | 18 |
| `debtEbitda` | 68.0% | 33 |
| `evEbitda` | 53.4% | 48 |
| `pb` | 2.9% | 100 |

- Top fail gate combinations (failed gates only):

  - `nearLow,evEbitda,pb`: 22
  - `nearLow,pb`: 15
  - `pb`: 14
  - `evEbitda,pb`: 9
  - `debtEbitda,evEbitda,pb`: 4
  - `de,netDe,debtEbitda,pb`: 4
  - `nearLow,de,netDe,debtEbitda,pb`: 4
  - `nearLow,de,debtEbitda,evEbitda,pb`: 3

- Sample passers: *(none)*

#### Pariah

- Pass: **0** / 103 complete (**0.00%**); all-list rate 0.00% including 0 incomplete
- Gate pass rates (among complete):

| Gate | Pass % | Fail count |
|------|-------:|-----------:|
| `industry` | 5.8% | 97 |
| `depressed_price` | 51.5% | 50 |
| `clean_bs` | 38.8% | 63 |
| `cheap_vs_capital` | 7.8% | 95 |

- Top fail gate combinations (failed gates only):

  - `industry,clean_bs,cheap_vs_capital`: 27
  - `industry,depressed_price,clean_bs,cheap_vs_capital`: 23
  - `industry,depressed_price,cheap_vs_capital`: 22
  - `industry,cheap_vs_capital`: 17
  - `industry,clean_bs`: 5
  - `clean_bs,cheap_vs_capital`: 3
  - `depressed_price,clean_bs,cheap_vs_capital`: 3
  - `industry,depressed_price,clean_bs`: 2

- Sample passers: *(none)*

### Asia Emerging businesses

- `list_id`: `Asia Emerging businesses.csv`
- Universe loaded: **1088** (pending YF: **0**)
- Peer EV/EBITDA: median **7.8** over 751 names with positive multiple

#### Burry

- Pass: **97** / 1088 complete (**8.92%**); all-list rate 8.92% including 0 incomplete
- Gate pass rates (among complete):

| Gate | Pass % | Fail count |
|------|-------:|-----------:|
| `nearLow` | 49.4% | 550 |
| `fcf` | 50.2% | 542 |
| `cheap` | 36.9% | 686 |
| `debt` | 68.9% | 338 |
| `evEbitda` | 34.8% | 709 |

- Top fail gate combinations (failed gates only):

  - `nearLow,fcf,cheap,evEbitda`: 143
  - `fcf,cheap,evEbitda`: 113
  - `nearLow,fcf,cheap,debt,evEbitda`: 73
  - `fcf,cheap,debt,evEbitda`: 72
  - `nearLow`: 72
  - `nearLow,evEbitda`: 65
  - `nearLow,fcf,cheap`: 56
  - `nearLow,cheap,evEbitda`: 54

- Sample passers: SNFRY, UNPSF, UPCHY, WIT, XTEPY, PHDWY, NGCRY, PITPY, MAOFF, PTXKY, PTKFY, TPSRF, MATH, NGCRF, MAANF

#### Templeton

- Pass: **118** / 1088 complete (**10.85%**); all-list rate 10.85% including 0 incomplete
- Gate pass rates (among complete):

| Gate | Pass % | Fail count |
|------|-------:|-----------:|
| `nearLow` | 62.1% | 412 |
| `de` | 75.8% | 263 |
| `netDe` | 78.8% | 231 |
| `debtEbitda` | 55.6% | 483 |
| `evEbitda` | 34.8% | 709 |
| `pb` | 61.1% | 423 |

- Top fail gate combinations (failed gates only):

  - `evEbitda`: 97
  - `evEbitda,pb`: 92
  - `nearLow,evEbitda,pb`: 79
  - `nearLow,evEbitda`: 63
  - `de,netDe,debtEbitda,evEbitda`: 59
  - `nearLow`: 56
  - `debtEbitda,evEbitda`: 54
  - `pb`: 53

- Sample passers: SNFRY, JFIN, XTEPY, OROVY, NGCRY, MAOFF, SVTMF, PTKFY, TPSRF, MATH, NGCRF, PGCMF, ULIHF, CHHQY, TRPCF

#### Pariah

- Pass: **53** / 1088 complete (**4.87%**); all-list rate 4.87% including 0 incomplete
- Gate pass rates (among complete):

| Gate | Pass % | Fail count |
|------|-------:|-----------:|
| `industry` | 17.3% | 900 |
| `depressed_price` | 76.0% | 261 |
| `clean_bs` | 57.7% | 460 |
| `cheap_vs_capital` | 68.3% | 345 |

- Top fail gate combinations (failed gates only):

  - `industry`: 290
  - `industry,clean_bs`: 215
  - `industry,cheap_vs_capital`: 128
  - `industry,clean_bs,cheap_vs_capital`: 70
  - `industry,depressed_price,cheap_vs_capital`: 61
  - `industry,depressed_price`: 59
  - `clean_bs`: 50
  - `industry,depressed_price,clean_bs`: 44

- Sample passers: SNFRY, PECGF, OROVY, PITPY, DQ, CHYMF, PAEKY, AHCHY, SPTJF, TBNGY, PPAAY, BON, CCOZF, ITP, QNDPF

### Australasian business url list

- `list_id`: `Australasian business url list.csv`
- Universe loaded: **1316** (pending YF: **0**)
- Peer EV/EBITDA: median **10.9** over 489 names with positive multiple

#### Burry

- Pass: **40** / 1316 complete (**3.04%**); all-list rate 3.04% including 0 incomplete
- Gate pass rates (among complete):

| Gate | Pass % | Fail count |
|------|-------:|-----------:|
| `nearLow` | 38.9% | 804 |
| `fcf` | 36.2% | 840 |
| `cheap` | 22.2% | 1024 |
| `debt` | 82.9% | 225 |
| `evEbitda` | 18.9% | 1067 |

- Top fail gate combinations (failed gates only):

  - `nearLow,fcf,cheap,evEbitda`: 414
  - `fcf,cheap,evEbitda`: 275
  - `nearLow,cheap,evEbitda`: 96
  - `nearLow`: 71
  - `nearLow,evEbitda`: 58
  - `fcf,cheap,debt,evEbitda`: 51
  - `nearLow,fcf,cheap,debt,evEbitda`: 49
  - `nearLow,debt`: 36

- Sample passers: HSN.AX, GLB.AX, GDF.AX, CAA.AX, TPG.AX, SFC.AX, MAM.AX, SPZ.AX, DXS.AX, CHL.AX, COS.AX, TLS.AX, MGR.AX, TWD.AX, REG.AX

#### Templeton

- Pass: **64** / 1316 complete (**4.86%**); all-list rate 4.86% including 0 incomplete
- Gate pass rates (among complete):

| Gate | Pass % | Fail count |
|------|-------:|-----------:|
| `nearLow` | 52.6% | 624 |
| `de` | 85.5% | 191 |
| `netDe` | 89.4% | 139 |
| `debtEbitda` | 72.0% | 369 |
| `evEbitda` | 18.9% | 1067 |
| `pb` | 42.9% | 752 |

- Top fail gate combinations (failed gates only):

  - `nearLow,evEbitda,pb`: 283
  - `evEbitda,pb`: 195
  - `evEbitda`: 165
  - `nearLow,evEbitda`: 119
  - `debtEbitda,evEbitda`: 65
  - `pb`: 38
  - `nearLow,pb`: 35
  - `nearLow,de,debtEbitda,evEbitda,pb`: 34

- Sample passers: SSL.AX, KSC.AX, GLB.AX, FFI.AX, CAA.AX, ASB.AX, CHL.AX, FRI.AX, ANG.AX, EGG.AX, MXI.AX, CCR.AX, CWP.AX, PFG.AX, TRP.AX

#### Pariah

- Pass: **186** / 1316 complete (**14.13%**); all-list rate 14.13% including 0 incomplete
- Gate pass rates (among complete):

| Gate | Pass % | Fail count |
|------|-------:|-----------:|
| `industry` | 47.4% | 692 |
| `depressed_price` | 71.3% | 378 |
| `clean_bs` | 70.3% | 391 |
| `cheap_vs_capital` | 42.8% | 753 |

- Top fail gate combinations (failed gates only):

  - `depressed_price,cheap_vs_capital`: 167
  - `industry,cheap_vs_capital`: 157
  - `cheap_vs_capital`: 152
  - `industry`: 140
  - `industry,clean_bs`: 139
  - `industry,clean_bs,cheap_vs_capital`: 105
  - `industry,depressed_price,cheap_vs_capital`: 81
  - `industry,depressed_price,clean_bs,cheap_vs_capital`: 44

- Sample passers: VUL.AX, FAR.AX, AVL.AX, HAS.AX, NFL.AX, LEL.AX, BPT.AX, BRL.AX, GWR.AX, MGL.AX, VAL.AX, TBR.AX, ARL.AX, AKO.AX, EME.AX

### Energy businesses list

- `list_id`: `Energy businesses list.csv`
- Universe loaded: **156** (pending YF: **0**)
- Peer EV/EBITDA: median **8.7** over 131 names with positive multiple

#### Burry

- Pass: **3** / 156 complete (**1.92%**); all-list rate 1.92% including 0 incomplete
- Gate pass rates (among complete):

| Gate | Pass % | Fail count |
|------|-------:|-----------:|
| `nearLow` | 14.7% | 133 |
| `fcf` | 64.1% | 56 |
| `cheap` | 39.1% | 95 |
| `debt` | 55.1% | 70 |
| `evEbitda` | 41.7% | 91 |

- Top fail gate combinations (failed gates only):

  - `nearLow`: 17
  - `nearLow,fcf,cheap,evEbitda`: 17
  - `nearLow,cheap,debt,evEbitda`: 16
  - `nearLow,debt`: 16
  - `nearLow,fcf,cheap,debt,evEbitda`: 14
  - `nearLow,evEbitda`: 11
  - `nearLow,fcf,cheap`: 9
  - `nearLow,debt,evEbitda`: 9

- Sample passers: GPOR, EQT, ARLP

#### Templeton

- Pass: **9** / 156 complete (**5.77%**); all-list rate 5.77% including 0 incomplete
- Gate pass rates (among complete):

| Gate | Pass % | Fail count |
|------|-------:|-----------:|
| `nearLow` | 25.6% | 116 |
| `de` | 71.8% | 44 |
| `netDe` | 76.3% | 37 |
| `debtEbitda` | 59.6% | 63 |
| `evEbitda` | 41.7% | 91 |
| `pb` | 33.3% | 104 |

- Top fail gate combinations (failed gates only):

  - `nearLow,evEbitda,pb`: 23
  - `nearLow`: 20
  - `nearLow,de,netDe,debtEbitda,evEbitda,pb`: 16
  - `nearLow,pb`: 14
  - `evEbitda,pb`: 9
  - `nearLow,debtEbitda,evEbitda`: 7
  - `nearLow,de,debtEbitda,evEbitda,pb`: 6
  - `nearLow,debtEbitda,evEbitda,pb`: 6

- Sample passers: VTS, CRDE, EQT, GRNT, UNTC, TPET, AMPY, AR, CNX

#### Pariah

- Pass: **11** / 156 complete (**7.05%**); all-list rate 7.05% including 0 incomplete
- Gate pass rates (among complete):

| Gate | Pass % | Fail count |
|------|-------:|-----------:|
| `industry` | 100.0% | 0 |
| `depressed_price` | 41.0% | 92 |
| `clean_bs` | 32.1% | 106 |
| `cheap_vs_capital` | 32.1% | 106 |

- Top fail gate combinations (failed gates only):

  - `depressed_price,clean_bs,cheap_vs_capital`: 53
  - `clean_bs,cheap_vs_capital`: 23
  - `cheap_vs_capital`: 16
  - `depressed_price,clean_bs`: 16
  - `clean_bs`: 14
  - `depressed_price,cheap_vs_capital`: 14
  - `depressed_price`: 9

- Sample passers: VTS, GEOS, UNTC, TPET, AMPY, PRT, BOOM, OSG, CWPE, HGTXU, MRGE

### japan stocks

- `list_id`: `japan_stocks.csv`
- Universe loaded: **3548** (pending YF: **0**)
- Peer EV/EBITDA: median **7.2** over 3122 names with positive multiple

#### Burry

- Pass: **225** / 3548 complete (**6.34%**); all-list rate 6.34% including 0 incomplete
- Gate pass rates (among complete):

| Gate | Pass % | Fail count |
|------|-------:|-----------:|
| `nearLow` | 40.5% | 2110 |
| `fcf` | 45.9% | 1918 |
| `cheap` | 26.8% | 2598 |
| `debt` | 75.8% | 858 |
| `evEbitda` | 44.3% | 1978 |

- Top fail gate combinations (failed gates only):

  - `nearLow,fcf,cheap,evEbitda`: 515
  - `nearLow`: 317
  - `fcf,cheap,evEbitda`: 292
  - `fcf,cheap`: 278
  - `nearLow,cheap,evEbitda`: 276
  - `nearLow,fcf,cheap`: 267
  - `nearLow,fcf,cheap,debt,evEbitda`: 239
  - `fcf,cheap,debt,evEbitda`: 180

- Sample passers: 8275.T, 2375.T, 6035.T, 7555.T, 5446.T, 5842.T, 7284.T, 8226.T, 9414.T, 8166.T, 4198.T, 4365.T, 7464.T, 4628.T, 7820.T

#### Templeton

- Pass: **662** / 3548 complete (**18.66%**); all-list rate 18.66% including 0 incomplete
- Gate pass rates (among complete):

| Gate | Pass % | Fail count |
|------|-------:|-----------:|
| `nearLow` | 59.1% | 1452 |
| `de` | 83.7% | 578 |
| `netDe` | 89.0% | 392 |
| `debtEbitda` | 68.5% | 1119 |
| `evEbitda` | 44.3% | 1978 |
| `pb` | 59.8% | 1425 |

- Top fail gate combinations (failed gates only):

  - `nearLow,evEbitda,pb`: 386
  - `evEbitda,pb`: 308
  - `nearLow`: 295
  - `evEbitda`: 223
  - `pb`: 220
  - `nearLow,evEbitda`: 204
  - `debtEbitda,evEbitda`: 144
  - `nearLow,debtEbitda,evEbitda`: 114

- Sample passers: 8275.T, 6198.T, 7809.T, 2329.T, 4057.T, 3998.T, 7351.T, 7636.T, 2375.T, 7555.T, 5446.T, 7422.T, 6255.T, 5842.T, 2911.T

#### Pariah

- Pass: **84** / 3548 complete (**2.37%**); all-list rate 2.37% including 0 incomplete
- Gate pass rates (among complete):

| Gate | Pass % | Fail count |
|------|-------:|-----------:|
| `industry` | 9.4% | 3215 |
| `depressed_price` | 67.4% | 1155 |
| `clean_bs` | 64.1% | 1272 |
| `cheap_vs_capital` | 49.0% | 1809 |

- Top fail gate combinations (failed gates only):

  - `industry,cheap_vs_capital`: 807
  - `industry`: 652
  - `industry,clean_bs`: 440
  - `industry,depressed_price,cheap_vs_capital`: 390
  - `industry,clean_bs,cheap_vs_capital`: 313
  - `industry,depressed_price`: 255
  - `industry,depressed_price,clean_bs,cheap_vs_capital`: 217
  - `industry,depressed_price,clean_bs`: 141

- Sample passers: 5446.T, 6239.T, 4365.T, 4628.T, 5921.T, 4976.T, 4094.T, 7531.T, 4624.T, 5357.T, 5284.T, 3895.T, 9818.T, 2613.T, 5464.T

### korea stocks

- `list_id`: `korea_stocks.csv`
- Universe loaded: **2682** (pending YF: **2000**)
- Peer EV/EBITDA: median **6.4** over 495 names with positive multiple

#### Burry

- Pass: **69** / 682 complete (**10.12%**); all-list rate 2.57% including 2000 incomplete
- Gate pass rates (among complete):

| Gate | Pass % | Fail count |
|------|-------:|-----------:|
| `nearLow` | 68.8% | 213 |
| `fcf` | 45.7% | 370 |
| `cheap` | 37.1% | 429 |
| `debt` | 60.0% | 273 |
| `evEbitda` | 36.8% | 431 |

- Top fail gate combinations (failed gates only):

  - `fcf,cheap,evEbitda`: 108
  - `fcf,cheap,debt,evEbitda`: 82
  - `nearLow,fcf,cheap,evEbitda`: 62
  - `evEbitda`: 44
  - `nearLow,fcf,cheap,debt,evEbitda`: 41
  - `debt`: 39
  - `debt,evEbitda`: 37
  - `fcf,cheap`: 37

- Sample passers: 011785.KS, 000320.KS, 000325.KS, 007720.KQ, 005500.KS, 006140.KQ, 014580.KS, 004490.KS, 016740.KS, 024900.KS, 004560.KS, 005740.KS, 003310.KQ, 003850.KS, 003200.KS

#### Templeton

- Pass: **0** / 682 complete (**0.00%**); all-list rate 0.00% including 2000 incomplete
- Gate pass rates (among complete):

| Gate | Pass % | Fail count |
|------|-------:|-----------:|
| `nearLow` | 80.1% | 136 |
| `de` | 78.0% | 150 |
| `netDe` | 100.0% | 0 |
| `debtEbitda` | 46.2% | 367 |
| `evEbitda` | 36.8% | 431 |
| `pb` | 0.0% | 682 |

- Top fail gate combinations (failed gates only):

  - `evEbitda,pb`: 128
  - `pb`: 122
  - `debtEbitda,evEbitda,pb`: 117
  - `de,debtEbitda,evEbitda,pb`: 91
  - `debtEbitda,pb`: 59
  - `nearLow,debtEbitda,evEbitda,pb`: 37
  - `nearLow,pb`: 34
  - `nearLow,evEbitda,pb`: 30

- Sample passers: *(none)*

#### Pariah

- Pass: **57** / 682 complete (**8.36%**); all-list rate 2.13% including 2000 incomplete
- Gate pass rates (among complete):

| Gate | Pass % | Fail count |
|------|-------:|-----------:|
| `industry` | 21.8% | 533 |
| `depressed_price` | 85.2% | 101 |
| `clean_bs` | 47.5% | 358 |
| `cheap_vs_capital` | 100.0% | 0 |

- Top fail gate combinations (failed gates only):

  - `industry,clean_bs`: 227
  - `industry`: 216
  - `clean_bs`: 81
  - `industry,depressed_price`: 46
  - `industry,depressed_price,clean_bs`: 44
  - `depressed_price,clean_bs`: 6
  - `depressed_price`: 5

- Sample passers: 017390.KS, 018310.KQ, 011785.KS, 014190.KQ, 000320.KS, 000325.KS, 007590.KS, 004560.KS, 026940.KS, 005810.KS, 006890.KS, 002310.KS, 011500.KS, 021040.KQ, 004690.KS

### Large core businesses list

- `list_id`: `Large core businesses list.csv`
- Universe loaded: **91** (pending YF: **0**)
- Peer EV/EBITDA: median **16.1** over 86 names with positive multiple

#### Burry

- Pass: **2** / 91 complete (**2.20%**); all-list rate 2.20% including 0 incomplete
- Gate pass rates (among complete):

| Gate | Pass % | Fail count |
|------|-------:|-----------:|
| `nearLow` | 23.1% | 70 |
| `fcf` | 80.2% | 18 |
| `cheap` | 17.6% | 75 |
| `debt` | 63.7% | 33 |
| `evEbitda` | 49.5% | 46 |

- Top fail gate combinations (failed gates only):

  - `nearLow,cheap,evEbitda`: 17
  - `nearLow,cheap`: 12
  - `nearLow,cheap,debt`: 8
  - `nearLow,cheap,debt,evEbitda`: 8
  - `nearLow,fcf,cheap,evEbitda`: 7
  - `nearLow`: 7
  - `cheap`: 4
  - `cheap,evEbitda`: 4

- Sample passers: YUMC, GIS

#### Templeton

- Pass: **0** / 91 complete (**0.00%**); all-list rate 0.00% including 0 incomplete
- Gate pass rates (among complete):

| Gate | Pass % | Fail count |
|------|-------:|-----------:|
| `nearLow` | 49.5% | 46 |
| `de` | 52.7% | 43 |
| `netDe` | 57.1% | 39 |
| `debtEbitda` | 58.2% | 38 |
| `evEbitda` | 49.5% | 46 |
| `pb` | 3.3% | 88 |

- Top fail gate combinations (failed gates only):

  - `nearLow,evEbitda,pb`: 11
  - `nearLow,pb`: 9
  - `de,netDe,debtEbitda,pb`: 8
  - `de,netDe,pb`: 6
  - `pb`: 6
  - `nearLow,de,netDe,debtEbitda,evEbitda,pb`: 5
  - `nearLow,de,netDe,pb`: 5
  - `evEbitda,pb`: 4

- Sample passers: *(none)*

#### Pariah

- Pass: **0** / 91 complete (**0.00%**); all-list rate 0.00% including 0 incomplete
- Gate pass rates (among complete):

| Gate | Pass % | Fail count |
|------|-------:|-----------:|
| `industry` | 13.2% | 79 |
| `depressed_price` | 57.1% | 39 |
| `clean_bs` | 15.4% | 77 |
| `cheap_vs_capital` | 7.7% | 84 |

- Top fail gate combinations (failed gates only):

  - `industry,clean_bs,cheap_vs_capital`: 36
  - `industry,depressed_price,clean_bs,cheap_vs_capital`: 24
  - `industry,cheap_vs_capital`: 7
  - `depressed_price,clean_bs,cheap_vs_capital`: 7
  - `industry,depressed_price,cheap_vs_capital`: 5
  - `industry,clean_bs`: 4
  - `clean_bs,cheap_vs_capital`: 4
  - `industry,depressed_price,clean_bs`: 2

- Sample passers: *(none)*

### Large growth businesses list

- `list_id`: `Large growth businesses list.csv`
- Universe loaded: **64** (pending YF: **0**)
- Peer EV/EBITDA: median **20.6** over 61 names with positive multiple

#### Burry

- Pass: **4** / 64 complete (**6.25%**); all-list rate 6.25% including 0 incomplete
- Gate pass rates (among complete):

| Gate | Pass % | Fail count |
|------|-------:|-----------:|
| `nearLow` | 34.4% | 42 |
| `fcf` | 90.6% | 6 |
| `cheap` | 21.9% | 50 |
| `debt` | 85.9% | 9 |
| `evEbitda` | 48.4% | 33 |

- Top fail gate combinations (failed gates only):

  - `nearLow,cheap,evEbitda`: 18
  - `nearLow,cheap`: 11
  - `cheap`: 7
  - `cheap,evEbitda`: 5
  - `nearLow`: 4
  - `debt`: 3
  - `nearLow,fcf,cheap,evEbitda`: 3
  - `nearLow,cheap,debt,evEbitda`: 2

- Sample passers: NFLX, LULU, CRM, ADSK

#### Templeton

- Pass: **0** / 64 complete (**0.00%**); all-list rate 0.00% including 0 incomplete
- Gate pass rates (among complete):

| Gate | Pass % | Fail count |
|------|-------:|-----------:|
| `nearLow` | 56.2% | 28 |
| `de` | 65.6% | 22 |
| `netDe` | 76.6% | 15 |
| `debtEbitda` | 73.4% | 17 |
| `evEbitda` | 48.4% | 33 |
| `pb` | 1.6% | 63 |

- Top fail gate combinations (failed gates only):

  - `pb`: 13
  - `nearLow,evEbitda,pb`: 11
  - `evEbitda,pb`: 6
  - `nearLow,pb`: 6
  - `de,netDe,debtEbitda,pb`: 5
  - `debtEbitda,evEbitda,pb`: 2
  - `de,netDe,pb`: 2
  - `de,netDe,evEbitda,pb`: 2

- Sample passers: *(none)*

#### Pariah

- Pass: **0** / 64 complete (**0.00%**); all-list rate 0.00% including 0 incomplete
- Gate pass rates (among complete):

| Gate | Pass % | Fail count |
|------|-------:|-----------:|
| `industry` | 7.8% | 59 |
| `depressed_price` | 68.8% | 20 |
| `clean_bs` | 31.2% | 44 |
| `cheap_vs_capital` | 9.4% | 58 |

- Top fail gate combinations (failed gates only):

  - `industry,clean_bs,cheap_vs_capital`: 28
  - `industry,cheap_vs_capital`: 9
  - `industry,depressed_price,clean_bs,cheap_vs_capital`: 8
  - `industry,depressed_price,cheap_vs_capital`: 8
  - `industry,clean_bs`: 4
  - `clean_bs,cheap_vs_capital`: 2
  - `depressed_price,cheap_vs_capital`: 2
  - `industry`: 1

- Sample passers: *(none)*

### Large value businesses list

- `list_id`: `Large value businesses list.csv`
- Universe loaded: **53** (pending YF: **0**)
- Peer EV/EBITDA: median **11.9** over 43 names with positive multiple

#### Burry

- Pass: **1** / 53 complete (**1.89%**); all-list rate 1.89% including 0 incomplete
- Gate pass rates (among complete):

| Gate | Pass % | Fail count |
|------|-------:|-----------:|
| `nearLow` | 15.1% | 45 |
| `fcf` | 60.4% | 21 |
| `cheap` | 39.6% | 32 |
| `debt` | 45.3% | 29 |
| `evEbitda` | 43.4% | 30 |

- Top fail gate combinations (failed gates only):

  - `nearLow,fcf,cheap,debt,evEbitda`: 10
  - `nearLow,fcf,cheap,evEbitda`: 9
  - `nearLow,debt`: 7
  - `nearLow`: 7
  - `debt`: 4
  - `nearLow,cheap,debt,evEbitda`: 4
  - `nearLow,cheap,evEbitda`: 3
  - `nearLow,cheap,debt`: 2

- Sample passers: AIG

#### Templeton

- Pass: **1** / 53 complete (**1.89%**); all-list rate 1.89% including 0 incomplete
- Gate pass rates (among complete):

| Gate | Pass % | Fail count |
|------|-------:|-----------:|
| `nearLow` | 35.8% | 34 |
| `de` | 41.5% | 31 |
| `netDe` | 54.7% | 24 |
| `debtEbitda` | 45.3% | 29 |
| `evEbitda` | 43.4% | 30 |
| `pb` | 15.1% | 45 |

- Top fail gate combinations (failed gates only):

  - `nearLow,de,netDe,debtEbitda,evEbitda,pb`: 8
  - `nearLow,pb`: 5
  - `evEbitda,pb`: 4
  - `nearLow,evEbitda,pb`: 4
  - `nearLow,de,debtEbitda,evEbitda,pb`: 4
  - `de,netDe,debtEbitda,pb`: 3
  - `nearLow,de,netDe,debtEbitda,pb`: 3
  - `pb`: 2

- Sample passers: AIG

#### Pariah

- Pass: **0** / 53 complete (**0.00%**); all-list rate 0.00% including 0 incomplete
- Gate pass rates (among complete):

| Gate | Pass % | Fail count |
|------|-------:|-----------:|
| `industry` | 17.0% | 44 |
| `depressed_price` | 50.9% | 26 |
| `clean_bs` | 18.9% | 43 |
| `cheap_vs_capital` | 17.0% | 44 |

- Top fail gate combinations (failed gates only):

  - `industry,clean_bs,cheap_vs_capital`: 14
  - `industry,depressed_price,clean_bs,cheap_vs_capital`: 14
  - `clean_bs,cheap_vs_capital`: 5
  - `industry,clean_bs`: 4
  - `industry,depressed_price,cheap_vs_capital`: 4
  - `industry,cheap_vs_capital`: 3
  - `depressed_price,clean_bs,cheap_vs_capital`: 3
  - `industry,depressed_price,clean_bs`: 3

- Sample passers: *(none)*

### Materials businesses list

- `list_id`: `Materials businesses list.csv`
- Universe loaded: **121** (pending YF: **0**)
- Peer EV/EBITDA: median **11.3** over 103 names with positive multiple

#### Burry

- Pass: **0** / 121 complete (**0.00%**); all-list rate 0.00% including 0 incomplete
- Gate pass rates (among complete):

| Gate | Pass % | Fail count |
|------|-------:|-----------:|
| `nearLow` | 17.4% | 100 |
| `fcf` | 62.0% | 46 |
| `cheap` | 30.6% | 84 |
| `debt` | 65.3% | 42 |
| `evEbitda` | 43.0% | 69 |

- Top fail gate combinations (failed gates only):

  - `nearLow,cheap,evEbitda`: 18
  - `nearLow,fcf,cheap,evEbitda`: 17
  - `nearLow,debt`: 15
  - `nearLow`: 10
  - `fcf,cheap,evEbitda`: 8
  - `nearLow,fcf,cheap`: 6
  - `nearLow,evEbitda`: 6
  - `nearLow,cheap`: 6

- Sample passers: *(none)*

#### Templeton

- Pass: **4** / 121 complete (**3.31%**); all-list rate 3.31% including 0 incomplete
- Gate pass rates (among complete):

| Gate | Pass % | Fail count |
|------|-------:|-----------:|
| `nearLow` | 31.4% | 83 |
| `de` | 73.6% | 32 |
| `netDe` | 74.4% | 31 |
| `debtEbitda` | 59.5% | 49 |
| `evEbitda` | 43.0% | 69 |
| `pb` | 28.9% | 86 |

- Top fail gate combinations (failed gates only):

  - `nearLow,evEbitda,pb`: 24
  - `evEbitda,pb`: 14
  - `nearLow,pb`: 9
  - `nearLow,de,netDe,debtEbitda,pb`: 7
  - `nearLow`: 6
  - `nearLow,de,netDe,debtEbitda`: 6
  - `nearLow,debtEbitda,evEbitda,pb`: 6
  - `pb`: 5

- Sample passers: MOS, CMCL, BCC, CNR

#### Pariah

- Pass: **5** / 121 complete (**4.13%**); all-list rate 4.13% including 0 incomplete
- Gate pass rates (among complete):

| Gate | Pass % | Fail count |
|------|-------:|-----------:|
| `industry` | 99.2% | 1 |
| `depressed_price` | 45.5% | 66 |
| `clean_bs` | 32.2% | 82 |
| `cheap_vs_capital` | 20.7% | 96 |

- Top fail gate combinations (failed gates only):

  - `depressed_price,clean_bs,cheap_vs_capital`: 40
  - `clean_bs,cheap_vs_capital`: 22
  - `depressed_price,cheap_vs_capital`: 20
  - `clean_bs`: 14
  - `cheap_vs_capital`: 13
  - `depressed_price,clean_bs`: 5
  - `depressed_price`: 1
  - `industry,clean_bs,cheap_vs_capital`: 1

- Sample passers: MTAL, NTIC, CNR, AMR, HDSN

### Mid-cap core businesses list

- `list_id`: `Mid-cap core businesses list.csv`
- Universe loaded: **180** (pending YF: **0**)
- Peer EV/EBITDA: median **14.7** over 173 names with positive multiple

#### Burry

- Pass: **7** / 180 complete (**3.89%**); all-list rate 3.89% including 0 incomplete
- Gate pass rates (among complete):

| Gate | Pass % | Fail count |
|------|-------:|-----------:|
| `nearLow` | 18.3% | 147 |
| `fcf` | 78.3% | 39 |
| `cheap` | 31.7% | 123 |
| `debt` | 54.4% | 82 |
| `evEbitda` | 50.6% | 89 |

- Top fail gate combinations (failed gates only):

  - `nearLow,cheap,evEbitda`: 37
  - `nearLow,debt`: 22
  - `nearLow,cheap`: 13
  - `nearLow,cheap,debt,evEbitda`: 13
  - `nearLow`: 12
  - `nearLow,fcf,cheap,evEbitda`: 10
  - `nearLow,fcf,cheap,debt,evEbitda`: 9
  - `nearLow,cheap,debt`: 8

- Sample passers: DLB, OTIS, EQT, BR, GNTX, HII, AOS

#### Templeton

- Pass: **4** / 180 complete (**2.22%**); all-list rate 2.22% including 0 incomplete
- Gate pass rates (among complete):

| Gate | Pass % | Fail count |
|------|-------:|-----------:|
| `nearLow` | 44.4% | 100 |
| `de` | 57.8% | 76 |
| `netDe` | 56.7% | 78 |
| `debtEbitda` | 46.1% | 97 |
| `evEbitda` | 50.6% | 89 |
| `pb` | 11.7% | 159 |

- Top fail gate combinations (failed gates only):

  - `nearLow,evEbitda,pb`: 22
  - `pb`: 15
  - `de,netDe,debtEbitda,pb`: 15
  - `nearLow,pb`: 14
  - `de,netDe,debtEbitda,evEbitda,pb`: 10
  - `nearLow,de,netDe,debtEbitda,pb`: 10
  - `nearLow,de,netDe,debtEbitda,evEbitda,pb`: 10
  - `evEbitda,pb`: 8

- Sample passers: EQT, MKL, AR, CTSH

#### Pariah

- Pass: **0** / 180 complete (**0.00%**); all-list rate 0.00% including 0 incomplete
- Gate pass rates (among complete):

| Gate | Pass % | Fail count |
|------|-------:|-----------:|
| `industry` | 12.8% | 157 |
| `depressed_price` | 58.9% | 74 |
| `clean_bs` | 22.2% | 140 |
| `cheap_vs_capital` | 9.4% | 163 |

- Top fail gate combinations (failed gates only):

  - `industry,clean_bs,cheap_vs_capital`: 59
  - `industry,depressed_price,clean_bs,cheap_vs_capital`: 47
  - `industry,cheap_vs_capital`: 24
  - `industry,depressed_price,cheap_vs_capital`: 13
  - `clean_bs,cheap_vs_capital`: 12
  - `industry,clean_bs`: 9
  - `depressed_price,clean_bs,cheap_vs_capital`: 5
  - `industry,depressed_price,clean_bs`: 5

- Sample passers: *(none)*

### Mid-cap growth business url list

- `list_id`: `Mid-cap growth business url list.csv`
- Universe loaded: **125** (pending YF: **0**)
- Peer EV/EBITDA: median **20.3** over 109 names with positive multiple

#### Burry

- Pass: **3** / 125 complete (**2.40%**); all-list rate 2.40% including 0 incomplete
- Gate pass rates (among complete):

| Gate | Pass % | Fail count |
|------|-------:|-----------:|
| `nearLow` | 20.8% | 99 |
| `fcf` | 86.4% | 17 |
| `cheap` | 29.6% | 88 |
| `debt` | 80.8% | 24 |
| `evEbitda` | 48.8% | 64 |

- Top fail gate combinations (failed gates only):

  - `nearLow,cheap,evEbitda`: 34
  - `nearLow,cheap`: 15
  - `nearLow`: 14
  - `nearLow,fcf,cheap,evEbitda`: 9
  - `cheap`: 8
  - `nearLow,debt`: 7
  - `nearLow,evEbitda`: 7
  - `nearLow,cheap,debt`: 5

- Sample passers: TTD, RMD, PTC

#### Templeton

- Pass: **1** / 125 complete (**0.80%**); all-list rate 0.80% including 0 incomplete
- Gate pass rates (among complete):

| Gate | Pass % | Fail count |
|------|-------:|-----------:|
| `nearLow` | 36.8% | 79 |
| `de` | 67.2% | 41 |
| `netDe` | 79.2% | 26 |
| `debtEbitda` | 56.8% | 54 |
| `evEbitda` | 48.8% | 64 |
| `pb` | 4.8% | 119 |

- Top fail gate combinations (failed gates only):

  - `nearLow,evEbitda,pb`: 24
  - `pb`: 15
  - `nearLow,pb`: 15
  - `nearLow,debtEbitda,evEbitda,pb`: 8
  - `nearLow,de,debtEbitda,evEbitda,pb`: 7
  - `nearLow,de,netDe,debtEbitda,pb`: 7
  - `de,debtEbitda,evEbitda,pb`: 5
  - `evEbitda,pb`: 5

- Sample passers: EPAM

#### Pariah

- Pass: **0** / 125 complete (**0.00%**); all-list rate 0.00% including 0 incomplete
- Gate pass rates (among complete):

| Gate | Pass % | Fail count |
|------|-------:|-----------:|
| `industry` | 8.0% | 115 |
| `depressed_price` | 57.6% | 53 |
| `clean_bs` | 44.8% | 69 |
| `cheap_vs_capital` | 12.0% | 110 |

- Top fail gate combinations (failed gates only):

  - `industry,clean_bs,cheap_vs_capital`: 31
  - `industry,cheap_vs_capital`: 28
  - `industry,depressed_price,cheap_vs_capital`: 23
  - `industry,depressed_price,clean_bs,cheap_vs_capital`: 20
  - `industry,clean_bs`: 7
  - `industry,depressed_price,clean_bs`: 5
  - `clean_bs,cheap_vs_capital`: 3
  - `depressed_price,cheap_vs_capital`: 3

- Sample passers: *(none)*

### Mid-cap value business url list

- `list_id`: `Mid-cap value business url list.csv`
- Universe loaded: **135** (pending YF: **0**)
- Peer EV/EBITDA: median **10.2** over 110 names with positive multiple

#### Burry

- Pass: **3** / 135 complete (**2.22%**); all-list rate 2.22% including 0 incomplete
- Gate pass rates (among complete):

| Gate | Pass % | Fail count |
|------|-------:|-----------:|
| `nearLow` | 16.3% | 113 |
| `fcf` | 67.4% | 44 |
| `cheap` | 45.9% | 73 |
| `debt` | 46.7% | 72 |
| `evEbitda` | 42.2% | 78 |

- Top fail gate combinations (failed gates only):

  - `nearLow,fcf,cheap,evEbitda`: 17
  - `nearLow,debt`: 17
  - `nearLow`: 15
  - `nearLow,fcf,cheap,debt,evEbitda`: 15
  - `nearLow,cheap,evEbitda`: 12
  - `nearLow,debt,evEbitda`: 12
  - `nearLow,cheap`: 6
  - `fcf,cheap,debt,evEbitda`: 5

- Sample passers: LEN, INGR, FOXA

#### Templeton

- Pass: **6** / 135 complete (**4.44%**); all-list rate 4.44% including 0 incomplete
- Gate pass rates (among complete):

| Gate | Pass % | Fail count |
|------|-------:|-----------:|
| `nearLow` | 34.1% | 89 |
| `de` | 51.1% | 66 |
| `netDe` | 65.2% | 47 |
| `debtEbitda` | 48.9% | 69 |
| `evEbitda` | 42.2% | 78 |
| `pb` | 33.3% | 90 |

- Top fail gate combinations (failed gates only):

  - `nearLow,pb`: 14
  - `nearLow,evEbitda,pb`: 13
  - `de,netDe,debtEbitda,evEbitda,pb`: 9
  - `nearLow,de,debtEbitda,evEbitda`: 9
  - `nearLow,de,netDe,debtEbitda,pb`: 7
  - `pb`: 6
  - `nearLow,de,netDe,debtEbitda,evEbitda,pb`: 6
  - `de,netDe,debtEbitda,evEbitda`: 5

- Sample passers: LEN, INGR, TAP, UHS, MOS, MHK

#### Pariah

- Pass: **0** / 135 complete (**0.00%**); all-list rate 0.00% including 0 incomplete
- Gate pass rates (among complete):

| Gate | Pass % | Fail count |
|------|-------:|-----------:|
| `industry` | 16.3% | 113 |
| `depressed_price` | 45.9% | 73 |
| `clean_bs` | 14.8% | 115 |
| `cheap_vs_capital` | 20.7% | 107 |

- Top fail gate combinations (failed gates only):

  - `industry,depressed_price,clean_bs,cheap_vs_capital`: 42
  - `industry,clean_bs,cheap_vs_capital`: 28
  - `industry,clean_bs`: 16
  - `industry,depressed_price,cheap_vs_capital`: 11
  - `clean_bs,cheap_vs_capital`: 10
  - `depressed_price,clean_bs,cheap_vs_capital`: 10
  - `industry,depressed_price,clean_bs`: 7
  - `industry,cheap_vs_capital`: 6

- Sample passers: *(none)*

### Small cap core business url list

- `list_id`: `Small cap core business url list.csv`
- Universe loaded: **2171** (pending YF: **0**)
- Peer EV/EBITDA: median **10.8** over 1257 names with positive multiple

#### Burry

- Pass: **87** / 2171 complete (**4.01%**); all-list rate 4.01% including 0 incomplete
- Gate pass rates (among complete):

| Gate | Pass % | Fail count |
|------|-------:|-----------:|
| `nearLow` | 31.6% | 1484 |
| `fcf` | 50.1% | 1084 |
| `cheap` | 28.9% | 1544 |
| `debt` | 70.8% | 634 |
| `evEbitda` | 29.4% | 1533 |

- Top fail gate combinations (failed gates only):

  - `nearLow,fcf,cheap,evEbitda`: 476
  - `nearLow,cheap,evEbitda`: 221
  - `fcf,cheap,evEbitda`: 206
  - `nearLow,fcf,cheap,debt,evEbitda`: 144
  - `nearLow`: 126
  - `nearLow,evEbitda`: 108
  - `nearLow,debt`: 90
  - `nearLow,debt,evEbitda`: 78

- Sample passers: TTGT, FDDMF, ELMTY, SIGA, EVSBY, EGHSF, VETOF, NSNHF, PHGPY, MATH, HCKT, MNSKY, GFAI, BKE, SPOK

#### Templeton

- Pass: **123** / 2171 complete (**5.67%**); all-list rate 5.67% including 0 incomplete
- Gate pass rates (among complete):

| Gate | Pass % | Fail count |
|------|-------:|-----------:|
| `nearLow` | 40.9% | 1283 |
| `de` | 73.8% | 569 |
| `netDe` | 82.7% | 376 |
| `debtEbitda` | 60.7% | 854 |
| `evEbitda` | 29.4% | 1533 |
| `pb` | 37.5% | 1357 |

- Top fail gate combinations (failed gates only):

  - `nearLow,evEbitda,pb`: 433
  - `evEbitda,pb`: 167
  - `nearLow,evEbitda`: 152
  - `evEbitda`: 122
  - `nearLow,pb`: 109
  - `nearLow,de,debtEbitda,evEbitda,pb`: 93
  - `nearLow,de,netDe,debtEbitda,evEbitda,pb`: 92
  - `pb`: 80

- Sample passers: TTGT, CRFCF, FOCIF, NCRBF, LTGHF, AUTLF, SIGA, TPCFF, EGHSF, PLRX, FORTY, KROS, NSNHF, MATH, AVEXF

#### Pariah

- Pass: **99** / 2171 complete (**4.56%**); all-list rate 4.56% including 0 incomplete
- Gate pass rates (among complete):

| Gate | Pass % | Fail count |
|------|-------:|-----------:|
| `industry` | 23.4% | 1664 |
| `depressed_price` | 57.8% | 916 |
| `clean_bs` | 54.3% | 993 |
| `cheap_vs_capital` | 39.2% | 1320 |

- Top fail gate combinations (failed gates only):

  - `industry,clean_bs`: 273
  - `industry,depressed_price,cheap_vs_capital`: 265
  - `industry,depressed_price,clean_bs,cheap_vs_capital`: 259
  - `industry,cheap_vs_capital`: 242
  - `industry,clean_bs,cheap_vs_capital`: 237
  - `industry`: 237
  - `depressed_price,cheap_vs_capital`: 134
  - `cheap_vs_capital`: 87

- Sample passers: NCRBF, WEGYF, AKRRF, GROY, ODV, TKFHY, AZMTF, GLDG, USGDF, SKKAF, GEOS, NTIC, ORGN, GBR, PENMF

### Small cap growth business url list

- `list_id`: `Small cap growth business url list.csv`
- Universe loaded: **469** (pending YF: **0**)
- Peer EV/EBITDA: median **17.0** over 270 names with positive multiple

#### Burry

- Pass: **7** / 469 complete (**1.49%**); all-list rate 1.49% including 0 incomplete
- Gate pass rates (among complete):

| Gate | Pass % | Fail count |
|------|-------:|-----------:|
| `nearLow` | 22.0% | 366 |
| `fcf` | 56.5% | 204 |
| `cheap` | 24.9% | 352 |
| `debt` | 68.4% | 148 |
| `evEbitda` | 29.0% | 333 |

- Top fail gate combinations (failed gates only):

  - `nearLow,fcf,cheap,evEbitda`: 90
  - `nearLow,cheap,evEbitda`: 81
  - `nearLow,fcf,cheap,debt,evEbitda`: 33
  - `fcf,cheap,evEbitda`: 31
  - `nearLow,evEbitda`: 27
  - `nearLow,cheap`: 27
  - `nearLow,debt`: 25
  - `nearLow`: 24

- Sample passers: ARCT, GPOR, DH, RSSS, EXLS, GDYN, SAM

#### Templeton

- Pass: **11** / 469 complete (**2.35%**); all-list rate 2.35% including 0 incomplete
- Gate pass rates (among complete):

| Gate | Pass % | Fail count |
|------|-------:|-----------:|
| `nearLow` | 34.5% | 307 |
| `de` | 67.8% | 151 |
| `netDe` | 79.7% | 95 |
| `debtEbitda` | 53.1% | 220 |
| `evEbitda` | 29.0% | 333 |
| `pb` | 21.5% | 368 |

- Top fail gate combinations (failed gates only):

  - `nearLow,evEbitda,pb`: 101
  - `nearLow,de,debtEbitda,evEbitda,pb`: 37
  - `evEbitda,pb`: 34
  - `nearLow,pb`: 33
  - `nearLow,de,netDe,debtEbitda,evEbitda,pb`: 24
  - `pb`: 21
  - `nearLow,debtEbitda,evEbitda,pb`: 21
  - `de,debtEbitda,evEbitda,pb`: 17

- Sample passers: ARCT, ACON, FTFT, VTS, HGBL, IVDA, GDYN, INVE, XXII, LUCY, SBFM

#### Pariah

- Pass: **3** / 469 complete (**0.64%**); all-list rate 0.64% including 0 incomplete
- Gate pass rates (among complete):

| Gate | Pass % | Fail count |
|------|-------:|-----------:|
| `industry` | 10.2% | 421 |
| `depressed_price` | 57.8% | 198 |
| `clean_bs` | 51.8% | 226 |
| `cheap_vs_capital` | 26.9% | 343 |

- Top fail gate combinations (failed gates only):

  - `industry,depressed_price,cheap_vs_capital`: 92
  - `industry,cheap_vs_capital`: 83
  - `industry,clean_bs,cheap_vs_capital`: 70
  - `industry,depressed_price,clean_bs,cheap_vs_capital`: 67
  - `industry,clean_bs`: 48
  - `industry`: 41
  - `industry,depressed_price,clean_bs`: 15
  - `clean_bs,cheap_vs_capital`: 10

- Sample passers: VTS, GBR, HDSN

### Small cap value business url list

- `list_id`: `Small cap value business url list.csv`
- Universe loaded: **1102** (pending YF: **0**)
- Peer EV/EBITDA: median **10.3** over 519 names with positive multiple

#### Burry

- Pass: **18** / 1102 complete (**1.63%**); all-list rate 1.63% including 0 incomplete
- Gate pass rates (among complete):

| Gate | Pass % | Fail count |
|------|-------:|-----------:|
| `nearLow` | 22.5% | 854 |
| `fcf` | 42.4% | 635 |
| `cheap` | 31.1% | 759 |
| `debt` | 53.7% | 510 |
| `evEbitda` | 24.0% | 837 |

- Top fail gate combinations (failed gates only):

  - `nearLow,fcf,cheap,evEbitda`: 269
  - `nearLow,fcf,cheap,debt,evEbitda`: 165
  - `nearLow,debt`: 80
  - `nearLow,debt,evEbitda`: 72
  - `fcf,cheap,evEbitda`: 66
  - `fcf,cheap,debt,evEbitda`: 66
  - `nearLow,evEbitda`: 54
  - `nearLow,cheap,evEbitda`: 52

- Sample passers: AAME, GNLN, NUS, GPOR, RKDA, STRA, CCLD, HNNA, ASLE, EHTH, ARLP, ITP, APT, PRDO, APAM

#### Templeton

- Pass: **45** / 1102 complete (**4.08%**); all-list rate 4.08% including 0 incomplete
- Gate pass rates (among complete):

| Gate | Pass % | Fail count |
|------|-------:|-----------:|
| `nearLow` | 34.3% | 724 |
| `de` | 56.4% | 480 |
| `netDe` | 73.3% | 294 |
| `debtEbitda` | 44.0% | 617 |
| `evEbitda` | 24.0% | 837 |
| `pb` | 52.5% | 523 |

- Top fail gate combinations (failed gates only):

  - `nearLow,evEbitda,pb`: 148
  - `nearLow,evEbitda`: 96
  - `de,netDe,debtEbitda,evEbitda`: 74
  - `nearLow,de,debtEbitda,evEbitda`: 71
  - `nearLow,de,netDe,debtEbitda,evEbitda,pb`: 64
  - `nearLow,de,debtEbitda,evEbitda,pb`: 64
  - `nearLow,de,netDe,debtEbitda,evEbitda`: 46
  - `evEbitda`: 45

- Sample passers: TNXP, RNA, FKWL, GNLN, CELZ, PLRX, NUS, NUWE, MBIO, NSPR, RKDA, STRA, KEQU, BMBL, EHTH

#### Pariah

- Pass: **8** / 1102 complete (**0.73%**); all-list rate 0.73% including 0 incomplete
- Gate pass rates (among complete):

| Gate | Pass % | Fail count |
|------|-------:|-----------:|
| `industry` | 8.0% | 1014 |
| `depressed_price` | 52.2% | 527 |
| `clean_bs` | 41.5% | 645 |
| `cheap_vs_capital` | 50.4% | 547 |

- Top fail gate combinations (failed gates only):

  - `industry,clean_bs`: 221
  - `industry,depressed_price,clean_bs,cheap_vs_capital`: 165
  - `industry,depressed_price,cheap_vs_capital`: 146
  - `industry`: 135
  - `industry,depressed_price,clean_bs`: 114
  - `industry,cheap_vs_capital`: 96
  - `industry,clean_bs,cheap_vs_capital`: 87
  - `industry,depressed_price`: 50

- Sample passers: SNES, AQMS, ITP, GEOS, PRT, TUSK, GURE, WWR

## Interpretation notes

- **Peer EV/EBITDA** is computed **within each list** (same as the dashboard). Sector median requires ≥5 peers with EV/EBITDA; otherwise the list global median is used.
- **Pending YF** rows fail closed (incomplete) for all three screens — high pending deflates all-list pass rates.
- **Pariah** is intentionally narrow (capital-cycle sector + clean BS + cheap book). Expect low pass rates on broad equity indices.
- **Burry** is often bottlenecked by near-low + FCF cheapness + EV/EBITDA together; check per-gate tables before retuning a single threshold.
- This report does **not** save UI presets or change defaults. Retune only after reviewing fail combos.

## Reproduce

```bash
python scripts/filter_pass_rate_report.py
```
