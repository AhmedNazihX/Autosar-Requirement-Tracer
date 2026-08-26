# Traceability report — module CanIf

- Corpus: `autosar-can` R23-11
- Code snapshot: `09433770bebb8f27a7b480d7c96d814c68ffed3e`
- Judge model: `openai/gpt-4o-mini`
- Cost: $0.0000 of a $2.00 ceiling

## Coverage

| status | requirements |
| --- | ---: |
| implemented | 98 |
| partial | 59 |
| missing | 177 |
| unverifiable | 64 |
| **judged** | **398 of 398** |

Implemented or partial: 157 of 398 judged (39.4%). 398 verdict(s) came from the cache and cost nothing.

## Matrix

| SRS (upstream) | SWS requirement | verdict | confidence | evidence |
| --- | --- | --- | ---: | --- |
| — | `SWS_CANIF_00378` | partial | 0.80 | `communication/CanIf/inc/CanIf_ConfigTypes.h:347-381`, `communication/CanIf/inc/CanIf_ConfigTypes.h:303-345` |
| — | `SWS_CANIF_00672` | implemented | 0.90 | `communication/CanIf/inc/CanIf.h:27-71` |
| `SRS_Can_01172` | `SWS_CANIF_00903` | missing | 1.00 | — |
| `SRS_Can_01001` | `SWS_CANIF_00023` | partial | 0.80 | `communication/CanIf/src/CanIf.c:82-84` |
| — | `SWS_CANIF_00291` | implemented | 0.90 | `communication/CanIf/inc/CanIf_ConfigTypes.h:134-159` |
| — | `SWS_CANIF_00662` | implemented | 0.90 | `communication/CanIf/inc/CanIf_ConfigTypes.h:182-197`, `communication/CanIf/inc/CanIf_ConfigTypes.h:161-180`, `communication/CanIf/inc/CanIf_ConfigTypes.h:134-159` |
| — | `SWS_CANIF_00663` | partial | 0.80 | `communication/CanIf/src/CanIf.c:1131-1132`, `communication/CanIf/inc/CanIf_ConfigTypes.h:134-159` |
| — | `SWS_CANIF_00664` | missing | 1.00 | — |
| — | `SWS_CANIF_00665` | unverifiable | 0.00 | — |
| — | `SWS_CANIF_00115` | missing | 0.90 | — |
| — | `SWS_CANIF_00292` | implemented | 0.90 | `communication/CanIf/inc/CanIf_ConfigTypes.h:161-180` |
| — | `SWS_CANIF_00466` | unverifiable | 0.90 | — |
| — | `SWS_CANIF_00666` | missing | 1.00 | — |
| — | `SWS_CANIF_00667` | unverifiable | 0.50 | — |
| — | `SWS_CANIF_00188` | missing | 1.00 | — |
| — | `SWS_CANIF_00673` | missing | 1.00 | — |
| — | `SWS_CANIF_00844` | missing | 1.00 | — |
| — | `SWS_CANIF_00854` | unverifiable | 0.90 | — |
| — | `SWS_CANIF_00855` | missing | 1.00 | — |
| — | `SWS_CANIF_00856` | unverifiable | 0.90 | — |
| — | `SWS_CANIF_00857` | missing | 1.00 | — |
| — | `SWS_CANIF_00653` | missing | 1.00 | — |
| — | `SWS_CANIF_00655` | missing | 1.00 | — |
| — | `SWS_CANIF_00847` | partial | 0.80 | `communication/CanIf/inc/CanIf_ConfigTypes.h:85-133`, `communication/CanIf/inc/CanIf_ConfigTypes.h:66-77`, `communication/CanIf/inc/CanIf_ConfigTypes.h:134-159` |
| — | `SWS_CANIF_00848` | missing | 1.00 | — |
| — | `SWS_CANIF_00211` | missing | 0.90 | — |
| `SRS_Can_01140` | `SWS_CANIF_00281` | unverifiable | 0.50 | — |
| — | `SWS_CANIF_00467` | unverifiable | 0.90 | — |
| — | `SWS_CANIF_00468` | partial | 0.80 | `communication/CanIf/inc/CanIf_ConfigTypes.h:134-159`, `communication/CanIf/inc/CanIf_ConfigTypes.h:161-180`, `communication/CanIf/inc/CanIf_ConfigTypes.h:182-197` |
| — | `SWS_CANIF_00469` | unverifiable | 0.50 | — |
| `SRS_Can_01140`, `SRS_Can_01141`, `SRS_Can_01162` | `SWS_CANIF_00877` | unverifiable | 0.90 | — |
| `SRS_Can_01126` | `SWS_CANIF_00382` | implemented | 0.90 | `communication/CanIf/src/CanIf.c:732-859` |
| `SRS_Can_01172` | `SWS_CANIF_00904` | unverifiable | 0.90 | — |
| `SRS_Can_01020` | `SWS_CANIF_00063` | partial | 0.70 | `communication/CanIf/src/CanIf.c:732-859`, `communication/CanIf/src/CanIf.c:861-932` |
| `SRS_Can_01126` | `SWS_CANIF_00381` | partial | 0.80 | `communication/CanIf/src/CanIf.c:732-859`, `communication/CanIf/src/CanIf.c:861-932` |
| — | `SWS_CANIF_00849` | partial | 0.80 | `communication/CanIf/inc/CanIf_ConfigTypes.h:228-281`, `communication/CanIf/src/CanIf.c:1208-1265` |
| `SRS_Can_01126` | `SWS_CANIF_00881` | missing | 1.00 | — |
| — | `SWS_CANIF_00895` | missing | 1.00 | — |
| `SRS_Can_01011` | `SWS_CANIF_00068` | missing | 1.00 | — |
| — | `SWS_CANIF_00835` | partial | 0.80 | `communication/CanIf/src/CanIf.c:732-859`, `communication/CanIf/inc/CanIf_ConfigTypes.h:199-208` |
| — | `SWS_CANIF_00836` | partial | 0.80 | `communication/CanIf/src/CanIf.c:336-378` |
| — | `SWS_CANIF_00837` | partial | 0.80 | `communication/CanIf/src/CanIf.c:732-859` |
| `SRS_Can_01114` | `SWS_CANIF_00033` | missing | 1.00 | — |
| — | `SWS_CANIF_00070` | partial | 0.80 | `communication/CanIf/src/CanIf.c:861-932`, `communication/CanIf/src/CanIf.c:732-859` |
| — | `SWS_CANIF_00183` | implemented | 0.90 | `communication/CanIf/src/CanIf.c:861-932` |
| — | `SWS_CANIF_00386` | implemented | 0.90 | `communication/CanIf/src/CanIf.c:861-932` |
| — | `SWS_CANIF_00387` | implemented | 1.00 | `communication/CanIf/src/CanIf.c:516-583` |
| — | `SWS_CANIF_00668` | partial | 0.80 | `communication/CanIf/src/CanIf.c:861-932` |
| — | `SWS_CANIF_00383` | implemented | 0.90 | `communication/CanIf/src/CanIf.c:861-932` |
| — | `SWS_CANIF_00740` | missing | 1.00 | — |
| `SRS_Can_01172` | `SWS_CANIF_00905` | missing | 1.00 | — |
| — | `SWS_CANIF_00056` | implemented | 0.90 | `communication/CanIf/src/CanIf.c:1170-1179` |
| `SRS_BSW_00325` | `SWS_CANIF_00135` | partial | 0.80 | `communication/CanIf/src/CanIf.c:1170-1179` |
| — | `SWS_CANIF_00297` | missing | 1.00 | — |
| — | `SWS_CANIF_00389` | missing | 1.00 | — |
| — | `SWS_CANIF_00390` | partial | 0.80 | `communication/CanIf/src/CanIf.c:1152-1157`, `communication/CanIf/src/CanIf.c:1170-1179` |
| — | `SWS_CANIF_00851` | missing | 1.00 | — |
| `SRS_Can_01172` | `SWS_CANIF_00906` | missing | 1.00 | — |
| — | `SWS_CANIF_00198` | missing | 1.00 | — |
| — | `SWS_CANIF_00199` | missing | 1.00 | — |
| `SRS_BSW_00312` | `SWS_CANIF_00064` | unverifiable | 0.90 | — |
| — | `SWS_CANIF_00472` | missing | 1.00 | — |
| — | `SWS_CANIF_00473` | missing | 1.00 | — |
| — | `SWS_CANIF_00485` | implemented | 1.00 | `communication/CanIf/src/CanIf.c:507-514` |
| `SRS_Can_01162`, `SRS_Can_02003` | `SWS_CANIF_00677` | partial | 0.80 | `communication/CanIf/src/CanIf.c:732-859` |
| — | `SWS_CANIF_00711` | partial | 0.80 | `communication/CanIf/src/CanIf.c:585-643` |
| — | `SWS_CANIF_00712` | partial | 0.80 | `communication/CanIf/src/CanIf.c:1626-1647`, `communication/CanSM/src/CanSM.c:1166-1194` |
| — | `SWS_CANIF_00724` | partial | 0.80 | `communication/CanIf/src/CanIf.c:1450-1495`, `communication/CanSM/src/CanSM.c:252-289` |
| — | `SWS_CANIF_00739` | missing | 1.00 | — |
| — | `SWS_CANIF_00395` | implemented | 0.90 | `communication/CanIf/src/CanIf.c:1654-1707` |
| — | `SWS_CANIF_00678` | partial | 0.80 | `communication/CanIf/src/CanIf.c:1654-1707` |
| — | `SWS_CANIF_00720` | implemented | 0.90 | `communication/CanIf/src/CanIf.c:1654-1707` |
| `SRS_Can_01136` | `SWS_CANIF_00179` | partial | 0.80 | `communication/CanIf/src/CanIf.c:1714-1793` |
| `SRS_Can_01151` | `SWS_CANIF_00286` | missing | 1.00 | — |
| — | `SWS_CANIF_00756` | partial | 0.80 | `communication/CanIf/src/CanIf.c:585-643` |
| — | `SWS_CANIF_00073` | partial | 0.80 | `communication/CanIf/src/CanIf.c:495-505`, `communication/CanIf/src/CanIf.c:732-859` |
| — | `SWS_CANIF_00864` | partial | 0.80 | `communication/CanIf/src/CanIf.c:516-583` |
| — | `SWS_CANIF_00865` | partial | 0.80 | `communication/CanIf/src/CanIf.c:645-711` |
| — | `SWS_CANIF_00866` | partial | 0.80 | `communication/CanIf/src/CanIf.c:645-711`, `communication/CanIf/src/CanIf.c:1450-1495` |
| — | `SWS_CANIF_00075` | implemented | 0.90 | `communication/CanIf/src/CanIf.c:732-859`, `communication/CanIf/src/CanIf.c:861-932`, `communication/CanIf/src/CanIf.c:1119-1124` |
| — | `SWS_CANIF_00118` | implemented | 0.90 | `communication/CanIf/src/CanIf.c:1450-1495` |
| — | `SWS_CANIF_00489` | partial | 0.80 | `communication/CanIf/src/CanIf.c:495-505`, `communication/CanIf/src/CanIf.c:732-859`, `communication/CanIf/src/CanIf.c:861-932` |
| — | `SWS_CANIF_00072` | partial | 0.80 | `communication/CanIf/src/CanIf.c:732-859` |
| `SRS_Can_02003` | `SWS_CANIF_00951` | unverifiable | 0.00 | — |
| `SRS_Can_02003` | `SWS_CANIF_00952` | missing | 1.00 | — |
| `SRS_Can_02003` | `SWS_CANIF_00953` | unverifiable | 0.00 | — |
| `SRS_Can_02003` | `SWS_CANIF_00954` | unverifiable | 0.00 | — |
| `SRS_Can_02003` | `SWS_CANIF_00955` | missing | 0.90 | — |
| `SRS_Can_02003` | `SWS_CANIF_00956` | unverifiable | 0.00 | — |
| `SRS_Can_01018` | `SWS_CANIF_00030` | unverifiable | 0.90 | — |
| — | `SWS_CANIF_00645` | implemented | 0.90 | `communication/CanIf/src/CanIf.c:947-999`, `communication/CanIf/src/CanIf.c:1001-1031` |
| — | `SWS_CANIF_00646` | partial | 0.80 | `communication/CanIf/src/CanIf.c:947-999`, `communication/CanIf/src/CanIf.c:1001-1031` |
| — | `SWS_CANIF_00852` | unverifiable | 0.00 | — |
| `SRS_Can_01005` | `SWS_CANIF_00026` | partial | 0.80 | `communication/CanIf/src/CanIf.c:1152-1157` |
| — | `SWS_CANIF_00168` | missing | 1.00 | — |
| — | `SWS_CANIF_00829` | partial | 0.80 | `communication/CanIf/src/CanIf.c:1170-1179` |
| — | `SWS_CANIF_00830` | unverifiable | 0.50 | — |
| — | `SWS_CANIF_00902` | unverifiable | 0.90 | — |
| — | `SWS_CANIF_00747` | partial | 0.80 | `communication/CanIf/src/CanIf.c:174-185`, `communication/CanIf/src/CanIf.c:1330-1338` |
| — | `SWS_CANIF_00748` | partial | 0.80 | `communication/CanIf/src/CanIf.c:516-583`, `communication/CanIf/src/CanIf.c:1330-1338` |
| — | `SWS_CANIF_00749` | partial | 0.80 | `communication/CanIf/src/CanIf.c:1330-1338`, `communication/CanIf/src/CanIf.c:645-711` |
| — | `SWS_CANIF_00750` | partial | 0.80 | `communication/CanIf/src/CanIf.c:732-859` |
| — | `SWS_CANIF_00751` | partial | 0.80 | `communication/CanIf/src/CanIf.c:861-932` |
| — | `SWS_CANIF_00863` | implemented | 0.90 | `communication/CanIf/src/CanIf.c:516-583` |
| — | `SWS_CANIF_00752` | partial | 0.80 | `communication/CanIf/src/CanIf.c:1341-1431` |
| — | `SWS_CANIF_00878` | partial | 0.80 | `communication/CanIf/src/CanIf.c:1341-1431` |
| — | `SWS_CANIF_00896` | missing | 1.00 | — |
| `RS_Ids_00810` | `SWS_CANIF_00913` | missing | 1.00 | — |
| `SRS_Can_02003` | `SWS_CANIF_00937` | missing | 1.00 | — |
| `SRS_Can_02003` | `SWS_CANIF_00938` | unverifiable | 0.00 | — |
| `RS_Ids_00810` | `SWS_CANIF_91010` | missing | 1.00 | — |
| `RS_Ids_00810` | `SWS_CANIF_00915` | missing | 1.00 | — |
| `RS_Ids_00810` | `SWS_CANIF_00916` | missing | 1.00 | — |
| `RS_Ids_00810` | `SWS_CANIF_00917` | missing | 0.90 | — |
| `RS_Ids_00810` | `SWS_CANIF_00918` | partial | 0.80 | `communication/CanIf/src/CanIf.c:1450-1495` |
| — | `SWS_CANIF_91006` | missing | 0.90 | — |
| — | `SWS_CANIF_91007` | missing | 1.00 | — |
| `SRS_BSW_00348`, `SRS_BSW_00353` | `SWS_CANIF_00142` | missing | 1.00 | — |
| — | `SWS_CANIF_00144` | implemented | 1.00 | `communication/CanIf/inc/CanIf_ConfigTypes.h:403-416` |
| — | `SWS_CANIF_00137` | missing | 1.00 | — |
| — | `SWS_CANIF_00523` | missing | 1.00 | — |
| `SRS_BSW_00405`, `SRS_BSW_00101`, `SRS_BSW_00358`, `SRS_BSW_00414`, `SRS_Can_01021`, `SRS_Can_01022` | `SWS_CANIF_00001` | implemented | 0.90 | `communication/CanIf/src/CanIf.c:516-583`, `communication/CanIf/inc/CanIf.h:136-137`, `communication/CanIf/inc/CanIf_ConfigTypes.h:403-416` |
| — | `SWS_CANIF_00201` | implemented | 1.00 | `communication/CanIf/inc/CanIf_Types.h:139-147` |
| — | `SWS_CANIF_00661` | partial | 0.80 | `communication/CanIf/src/CanIf.c:585-643`, `communication/CanIf/src/CanIf.c:645-711`, `communication/CanIf/src/CanIf.c:713-730`, `communication/CanIf/src/CanIf.c:732-859`, `communication/CanIf/src/CanIf.c:861-932`, `communication/CanIf/src/CanIf.c:1341-1431`, `communication/CanIf/src/CanIf.c:1433-1448`, `communication/CanIf/src/CanIf.c:1450-1495` |
| `SRS_Can_01027` | `SWS_CANIF_00003` | implemented | 0.90 | `communication/CanIf/src/CanIf.c:645-711` |
| — | `SWS_CANIF_00085` | implemented | 0.90 | `communication/CanIf/src/CanIf.c:516-583` |
| `SRS_Can_01168`, `SRS_BSW_00336` | `SWS_CANIF_91002` | missing | 1.00 | — |
| `SRS_Can_01028` | `SWS_CANIF_00229` | implemented | 1.00 | `communication/CanIf/src/CanIf.c:713-730` |
| — | `SWS_CANIF_00308` | implemented | 1.00 | `communication/CanIf/src/CanIf.c:645-711` |
| `SRS_BSW_00323` | `SWS_CANIF_00311` | implemented | 1.00 | `communication/CanIf/src/CanIf.c:645-711` |
| `SRS_BSW_00323` | `SWS_CANIF_00774` | implemented | 0.90 | `communication/CanIf/src/CanIf.c:645-711` |
| `SRS_BSW_00323` | `SWS_CANIF_00313` | implemented | 1.00 | `communication/CanIf/src/CanIf.c:713-730` |
| `SRS_BSW_00323` | `SWS_CANIF_00656` | implemented | 0.90 | `communication/CanIf/src/CanIf.c:713-730` |
| `SRS_BSW_00323` | `SWS_CANIF_00898` | missing | 1.00 | — |
| `SRS_Can_01169` | `SWS_CANIF_91001` | missing | 1.00 | — |
| `SRS_Can_01008` | `SWS_CANIF_00005` | implemented | 0.90 | `communication/CanIf/src/CanIf.c:732-859` |
| — | `SWS_CANIF_00317` | implemented | 0.90 | `communication/CanIf/src/CanIf.c:732-859` |
| — | `SWS_CANIF_00318` | partial | 0.80 | `communication/CanIf/src/CanIf.c:732-859`, `communication/CanIf/src/CanIf.c:861-932` |
| `SRS_BSW_00323` | `SWS_CANIF_00899` | missing | 1.00 | — |
| `SRS_Can_01162`, `SRS_Can_02003` | `SWS_CANIF_00939` | unverifiable | 0.90 | — |
| `SRS_Can_01162`, `SRS_Can_02003` | `SWS_CANIF_00162` | partial | 0.80 | `communication/CanIf/src/CanIf.c:732-859` |
| `SRS_Can_01141` | `SWS_CANIF_00243` | partial | 0.80 | `communication/CanIf/src/CanIf.c:732-859` |
| `SRS_BSW_00323` | `SWS_CANIF_00319` | implemented | 1.00 | `communication/CanIf/src/CanIf.c:732-859` |
| `SRS_BSW_00323` | `SWS_CANIF_00320` | implemented | 1.00 | `communication/CanIf/src/CanIf.c:732-859` |
| — | `SWS_CANIF_00882` | partial | 0.80 | `communication/CanIf/src/CanIf.c:732-859` |
| `SRS_Can_02003` | `SWS_CANIF_00957` | partial | 0.80 | `communication/CanIf/src/CanIf.c:732-859`, `communication/CanIf/src/CanIf.c:861-932` |
| `SRS_Can_01162`, `SRS_Can_02003` | `SWS_CANIF_00893` | partial | 0.80 | `communication/CanIf/src/CanIf.c:732-859` |
| — | `SWS_CANIF_00894` | partial | 0.80 | `communication/CanIf/src/CanIf.c:732-859` |
| — | `SWS_CANIF_00900` | missing | 1.00 | — |
| `SRS_Can_02003` | `SWS_CANIF_00940` | missing | 1.00 | — |
| `SRS_Can_02003` | `SWS_CANIF_00941` | missing | 1.00 | — |
| `SRS_Can_01125`, `SRS_Can_01129` | `SWS_CANIF_00194` | missing | 1.00 | — |
| — | `SWS_CANIF_00324` | missing | 1.00 | — |
| `SRS_BSW_00323` | `SWS_CANIF_00325` | missing | 1.00 | — |
| `SRS_Can_02003` | `SWS_CANIF_00942` | partial | 0.80 | `communication/CanIf/inc/CanIf_ConfigTypes.h:228-281`, `communication/CanIf/src/CanIf.c:1208-1265` |
| `SRS_Can_02003` | `SWS_CANIF_00943` | missing | 1.00 | — |
| `SRS_Can_01130` | `SWS_CANIF_00202` | missing | 1.00 | — |
| `SRS_BSW_00323` | `SWS_CANIF_00326` | missing | 1.00 | — |
| — | `SWS_CANIF_00329` | missing | 1.00 | — |
| — | `SWS_CANIF_00330` | missing | 1.00 | — |
| `SRS_BSW_00323` | `SWS_CANIF_00331` | missing | 1.00 | — |
| — | `SWS_CANIF_00393` | missing | 1.00 | — |
| `SRS_Can_01130`, `SRS_Can_01131` | `SWS_CANIF_00230` | missing | 1.00 | — |
| — | `SWS_CANIF_00335` | missing | 1.00 | — |
| `SRS_BSW_00323` | `SWS_CANIF_00336` | missing | 1.00 | — |
| — | `SWS_CANIF_00394` | missing | 1.00 | — |
| — | `SWS_CANIF_00008` | implemented | 0.90 | `communication/CanIf/src/CanIf.c:1341-1431` |
| — | `SWS_CANIF_00340` | missing | 1.00 | — |
| `SRS_BSW_00323` | `SWS_CANIF_00341` | implemented | 0.90 | `communication/CanIf/src/CanIf.c:1341-1431`, `communication/CanIf/src/CanIf.c:127-133` |
| `SRS_BSW_00323` | `SWS_CANIF_00860` | partial | 0.80 | `communication/CanIf/src/CanIf.c:1341-1431`, `communication/CanIf/src/CanIf.c:127-133` |
| — | `SWS_CANIF_00874` | partial | 0.80 | `communication/CanIf/src/CanIf.c:1341-1431` |
| — | `SWS_CANIF_00009` | implemented | 1.00 | `communication/CanIf/src/CanIf.c:1433-1448` |
| `SRS_BSW_00407`, `SRS_BSW_00411` | `SWS_CANIF_00158` | implemented | 0.90 | `communication/CanIf/inc/CanIf.h:185-188` |
| `SRS_BSW_00323` | `SWS_CANIF_00346` | partial | 0.80 | `communication/CanIf/src/CanIf.c:1433-1448` |
| `SRS_BSW_00323` | `SWS_CANIF_00657` | implemented | 1.00 | `communication/CanIf/src/CanIf.c:1433-1448` |
| — | `SWS_CANIF_00189` | missing | 1.00 | — |
| `SRS_BSW_00323` | `SWS_CANIF_00352` | missing | 1.00 | — |
| `SRS_BSW_00323` | `SWS_CANIF_00353` | missing | 1.00 | — |
| — | `SWS_CANIF_00355` | missing | 1.00 | — |
| `SRS_Can_02003` | `SWS_CANIF_00944` | missing | 1.00 | — |
| — | `SWS_CANIF_00287` | implemented | 1.00 | `communication/CanIf/src/CanIf.c:1537-1558` |
| — | `SWS_CANIF_00357` | missing | 1.00 | — |
| — | `SWS_CANIF_00358` | implemented | 1.00 | `communication/CanIf/src/CanIf.c:1537-1558` |
| `SRS_BSW_00323` | `SWS_CANIF_00538` | implemented | 1.00 | `communication/CanIf/src/CanIf.c:1537-1558` |
| — | `SWS_CANIF_00288` | implemented | 1.00 | `communication/CanIf/src/CanIf.c:1560-1580` |
| — | `SWS_CANIF_00362` | partial | 0.80 | `communication/CanIf/src/CanIf.c:1537-1558` |
| — | `SWS_CANIF_00363` | implemented | 1.00 | `communication/CanIf/src/CanIf.c:1560-1580` |
| `SRS_BSW_00323` | `SWS_CANIF_00648` | implemented | 1.00 | `communication/CanIf/src/CanIf.c:1537-1558` |
| — | `SWS_CANIF_00289` | implemented | 1.00 | `communication/CanIf/src/CanIf.c:1582-1601` |
| `SRS_BSW_00323` | `SWS_CANIF_00364` | implemented | 0.90 | `communication/CanIf/src/CanIf.c:1560-1580` |
| — | `SWS_CANIF_00367` | missing | 1.00 | — |
| `SRS_BSW_00323` | `SWS_CANIF_00650` | implemented | 1.00 | `communication/CanIf/src/CanIf.c:1560-1580` |
| — | `SWS_CANIF_00368` | implemented | 1.00 | `communication/CanIf/src/CanIf.c:1582-1601` |
| — | `SWS_CANIF_00371` | missing | 1.00 | — |
| `SRS_BSW_00323` | `SWS_CANIF_00537` | implemented | 1.00 | `communication/CanIf/src/CanIf.c:1582-1601` |
| `SRS_BSW_00323` | `SWS_CANIF_00649` | implemented | 1.00 | `communication/CanIf/src/CanIf.c:1582-1601` |
| — | `SWS_CANIF_00290` | implemented | 1.00 | `communication/CanIf/src/CanIf.c:1603-1624` |
| — | `SWS_CANIF_00372` | implemented | 1.00 | `communication/CanIf/src/CanIf.c:1603-1624` |
| — | `SWS_CANIF_00219` | implemented | 1.00 | `communication/CanIf/src/CanIf.c:1654-1707` |
| — | `SWS_CANIF_00373` | implemented | 1.00 | `communication/CanIf/src/CanIf.c:1603-1624` |
| `SRS_BSW_00323` | `SWS_CANIF_00535` | implemented | 0.90 | `communication/CanIf/src/CanIf.c:1603-1624` |
| `SRS_BSW_00323` | `SWS_CANIF_00536` | implemented | 1.00 | `communication/CanIf/src/CanIf.c:1603-1624` |
| — | `SWS_CANIF_00178` | implemented | 1.00 | `communication/CanIf/src/CanIf.c:1714-1793` |
| `SRS_BSW_00323` | `SWS_CANIF_00398` | implemented | 1.00 | `communication/CanIf/src/CanIf.c:1654-1707` |
| `SRS_BSW_00323` | `SWS_CANIF_00404` | implemented | 0.90 | `communication/CanIf/src/CanIf.c:1714-1793` |
| — | `SWS_CANIF_00408` | unverifiable | 0.90 | — |
| — | `SWS_CANIF_00734` | missing | 1.00 | — |
| — | `SWS_CANIF_00736` | missing | 1.00 | — |
| — | `SWS_CANIF_00738` | missing | 1.00 | — |
| — | `SWS_CANIF_00760` | implemented | 0.90 | `communication/CanIf/src/CanIf.c:1810-1826` |
| — | `SWS_CANIF_00761` | implemented | 1.00 | `communication/CanIf/src/CanIf.c:1831-1847` |
| — | `SWS_CANIF_00766` | implemented | 1.00 | `communication/CanIf/src/CanIf.c:1810-1826` |
| — | `SWS_CANIF_00769` | implemented | 1.00 | `communication/CanIf/src/CanIf.c:1810-1826` |
| — | `SWS_CANIF_00771` | missing | 1.00 | — |
| — | `SWS_CANIF_00765` | implemented | 1.00 | `communication/CanIf/src/CanIf.c:1831-1847` |
| — | `SWS_CANIF_00770` | implemented | 0.90 | `communication/CanIf/src/CanIf.c:1831-1847` |
| — | `SWS_CANIF_00813` | missing | 1.00 | — |
| — | `SWS_CANIF_00867` | missing | 1.00 | — |
| — | `SWS_CANIF_00868` | missing | 1.00 | — |
| `SRS_BSW_00323` | `SWS_CANIF_00869` | missing | 1.00 | — |
| — | `SWS_CANIF_00871` | missing | 1.00 | — |
| `SRS_BSW_00323` | `SWS_CANIF_00907` | missing | 1.00 | — |
| `SRS_BSW_00323` | `SWS_CANIF_00908` | missing | 1.00 | — |
| — | `SWS_CANIF_91003` | missing | 1.00 | — |
| `SRS_BSW_00323` | `SWS_CANIF_00909` | missing | 1.00 | — |
| `SRS_BSW_00323` | `SWS_CANIF_00910` | missing | 1.00 | — |
| — | `SWS_CANIF_91004` | missing | 1.00 | — |
| — | `SWS_CANIF_91005` | missing | 1.00 | — |
| `SRS_Can_01172` | `SWS_CANIF_00911` | missing | 1.00 | — |
| `SRS_BSW_00323` | `SWS_CANIF_00912` | partial | 0.80 | `communication/CanIf/src/CanIf.c:585-643`, `communication/CanIf/src/CanIf.c:1450-1495`, `communication/CanIf/src/CanIf.c:127-133` |
| `SRS_Can_01181` | `SWS_CANIF_91014` | missing | 1.00 | — |
| — | `SWS_CANIF_00922` | missing | 1.00 | — |
| — | `SWS_CANIF_00923` | missing | 1.00 | — |
| — | `SWS_CANIF_00924` | missing | 1.00 | — |
| — | `SWS_CANIF_00925` | unverifiable | 0.90 | — |
| — | `SWS_CANIF_00926` | missing | 1.00 | — |
| — | `SWS_CANIF_00927` | missing | 1.00 | — |
| — | `SWS_CANIF_00928` | unverifiable | 0.90 | — |
| `SRS_Can_01181` | `SWS_CANIF_91011` | missing | 1.00 | — |
| — | `SWS_CANIF_00929` | missing | 1.00 | — |
| — | `SWS_CANIF_00930` | missing | 1.00 | — |
| — | `SWS_CANIF_00931` | missing | 1.00 | — |
| — | `SWS_CANIF_00932` | unverifiable | 0.90 | — |
| `SRS_Can_01181` | `SWS_CANIF_91012` | missing | 1.00 | — |
| — | `SWS_CANIF_00933` | missing | 1.00 | — |
| — | `SWS_CANIF_00934` | unverifiable | 0.90 | — |
| — | `SWS_CANIF_00935` | missing | 1.00 | — |
| — | `SWS_CANIF_00936` | unverifiable | 0.90 | — |
| `SRS_Can_01181` | `SWS_CANIF_91013` | unverifiable | 0.00 | — |
| `SRS_Can_01009` | `SWS_CANIF_00007` | implemented | 1.00 | `communication/CanIf/src/CanIf.c:861-932` |
| — | `SWS_CANIF_00883` | missing | 1.00 | — |
| — | `SWS_CANIF_00884` | missing | 1.00 | — |
| — | `SWS_CANIF_00885` | missing | 1.00 | — |
| — | `SWS_CANIF_00391` | missing | 1.00 | — |
| `SRS_BSW_00323` | `SWS_CANIF_00410` | partial | 0.80 | `communication/CanIf/src/CanIf.c:861-932` |
| — | `SWS_CANIF_00412` | implemented | 0.90 | `communication/CanIf/src/CanIf.c:861-932` |
| — | `SWS_CANIF_00414` | missing | 1.00 | — |
| — | `SWS_CANIF_00006` | unverifiable | 0.90 | — |
| — | `SWS_CANIF_00392` | missing | 1.00 | — |
| — | `SWS_CANIF_00415` | partial | 0.80 | `communication/CanIf/src/CanIf.c:1170-1179` |
| `SRS_BSW_00323` | `SWS_CANIF_00416` | missing | 0.90 | — |
| `SRS_BSW_00323` | `SWS_CANIF_00417` | missing | 0.90 | — |
| `SRS_BSW_00323` | `SWS_CANIF_00419` | partial | 0.80 | `communication/CanIf/src/CanIf.c:1119-1124`, `communication/CanIf/src/CanIf.c:127-133` |
| — | `SWS_CANIF_00421` | implemented | 0.90 | `communication/CanIf/src/CanIf.c:1075-1094` |
| — | `SWS_CANIF_00423` | missing | 1.00 | — |
| — | `SWS_CANIF_91015` | unverifiable | 0.00 | — |
| — | `SWS_CANIF_00218` | implemented | 1.00 | `communication/CanIf/src/CanIf.c:1450-1495` |
| `SRS_Can_02003` | `SWS_CANIF_00945` | missing | 1.00 | — |
| `SRS_Can_02003` | `SWS_CANIF_00946` | missing | 1.00 | — |
| `SRS_Can_02003` | `SWS_CANIF_00947` | missing | 1.00 | — |
| `SRS_Can_02003` | `SWS_CANIF_00948` | unverifiable | 0.00 | — |
| `SRS_Can_02003` | `SWS_CANIF_00949` | missing | 1.00 | — |
| `SRS_Can_02003` | `SWS_CANIF_00950` | missing | 1.00 | — |
| `SRS_BSW_00323` | `SWS_CANIF_00429` | implemented | 0.90 | `communication/CanIf/src/CanIf.c:1450-1495` |
| — | `SWS_CANIF_00431` | implemented | 1.00 | `communication/CanIf/src/CanIf.c:1450-1495` |
| — | `SWS_CANIF_00433` | missing | 1.00 | — |
| — | `SWS_CANIF_00815` | implemented | 1.00 | `communication/CanIf/src/CanIf.c:1853-1873` |
| — | `SWS_CANIF_00753` | implemented | 1.00 | `communication/CanIf/src/CanIf.c:1853-1873` |
| — | `SWS_CANIF_00754` | missing | 1.00 | — |
| — | `SWS_CANIF_00816` | implemented | 1.00 | `communication/CanIf/src/CanIf.c:1853-1873` |
| — | `SWS_CANIF_00817` | implemented | 0.90 | `communication/CanIf/src/CanIf.c:1853-1873` |
| — | `SWS_CANIF_00963` | implemented | 1.00 | `communication/CanIf/src/CanIf.c:1853-1873` |
| — | `SWS_CANIF_91016` | implemented | 1.00 | `communication/CanIf/src/CanIf.c:1853-1873` |
| — | `SWS_CANIF_00757` | implemented | 1.00 | `communication/CanIf/src/CanIf.c:1879-1899` |
| — | `SWS_CANIF_00762` | implemented | 1.00 | `communication/CanIf/src/CanIf.c:1879-1899` |
| — | `SWS_CANIF_00805` | implemented | 0.90 | `communication/CanIf/src/CanIf.c:1879-1899` |
| — | `SWS_CANIF_00806` | implemented | 1.00 | `communication/CanIf/src/CanIf.c:1879-1899` |
| — | `SWS_CANIF_00964` | missing | 1.00 | — |
| — | `SWS_CANIF_00965` | implemented | 0.90 | `communication/CanIf/src/CanIf.c:1853-1873` |
| — | `SWS_CANIF_00966` | unverifiable | 0.90 | — |
| — | `SWS_CANIF_00759` | implemented | 1.00 | `communication/CanIf/src/CanIf.c:1904-1923` |
| — | `SWS_CANIF_00763` | implemented | 1.00 | `communication/CanIf/src/CanIf.c:1904-1923` |
| — | `SWS_CANIF_00808` | missing | 1.00 | — |
| — | `SWS_CANIF_00809` | implemented | 1.00 | `communication/CanIf/src/CanIf.c:1904-1923` |
| — | `SWS_CANIF_00810` | implemented | 0.90 | `communication/CanIf/src/CanIf.c:1904-1923` |
| — | `SWS_CANIF_00699` | implemented | 1.00 | `communication/CanIf/src/CanIf.c:585-643` |
| — | `SWS_CANIF_00700` | implemented | 0.90 | `communication/CanIf/src/CanIf.c:585-643` |
| — | `SWS_CANIF_00702` | partial | 0.80 | `communication/CanIf/src/CanIf.c:585-643` |
| — | `SWS_CANIF_00812` | unverifiable | 0.90 | — |
| — | `SWS_CANIF_00706` | implemented | 0.90 | `communication/CanIf/src/CanIf.c:1626-1647` |
| — | `SWS_CANIF_00708` | implemented | 1.00 | `communication/CanIf/src/CanIf.c:1626-1647` |
| — | `SWS_CANIF_00710` | implemented | 0.90 | `communication/CanIf/src/CanIf.c:1626-1647` |
| — | `SWS_CANIF_00730` | missing | 1.00 | — |
| — | `SWS_CANIF_00764` | implemented | 1.00 | `communication/CanIf/src/CanIf.c:1626-1647` |
| `RS_Ids_00810` | `SWS_CANIF_00919` | missing | 1.00 | — |
| `RS_Ids_00810` | `SWS_CANIF_91008` | missing | 1.00 | — |
| `RS_Ids_00810` | `SWS_CANIF_91009` | missing | 1.00 | — |
| `RS_Ids_00810` | `SWS_CANIF_00920` | missing | 1.00 | — |
| `RS_Ids_00810` | `SWS_CANIF_00921` | missing | 1.00 | — |
| — | `SWS_CANIF_00040` | missing | 1.00 | — |
| — | `SWS_CANIF_00294` | missing | 1.00 | — |
| — | `SWS_CANIF_00886` | missing | 1.00 | — |
| — | `SWS_CANIF_00888` | missing | 0.90 | — |
| — | `SWS_CANIF_00889` | missing | 1.00 | — |
| — | `SWS_CANIF_00011` | implemented | 0.90 | `communication/CanIf/src/CanIf.c:861-932` |
| — | `SWS_CANIF_00438` | missing | 0.90 | — |
| — | `SWS_CANIF_00890` | missing | 1.00 | — |
| — | `SWS_CANIF_00891` | missing | 1.00 | — |
| — | `SWS_CANIF_00439` | missing | 1.00 | — |
| — | `SWS_CANIF_00542` | unverifiable | 0.90 | — |
| — | `SWS_CANIF_00543` | unverifiable | 0.90 | — |
| — | `SWS_CANIF_00544` | missing | 1.00 | — |
| — | `SWS_CANIF_00550` | missing | 1.00 | — |
| — | `SWS_CANIF_00551` | unverifiable | 0.90 | — |
| — | `SWS_CANIF_00556` | unverifiable | 0.90 | — |
| — | `SWS_CANIF_00858` | missing | 1.00 | — |
| — | `SWS_CANIF_00879` | missing | 1.00 | — |
| `SRS_Can_01003` | `SWS_CANIF_00012` | missing | 0.90 | — |
| — | `SWS_CANIF_00441` | unverifiable | 0.90 | — |
| — | `SWS_CANIF_00552` | unverifiable | 0.90 | — |
| — | `SWS_CANIF_00442` | missing | 1.00 | — |
| — | `SWS_CANIF_00445` | unverifiable | 0.90 | — |
| — | `SWS_CANIF_00448` | missing | 1.00 | — |
| — | `SWS_CANIF_00532` | missing | 1.00 | — |
| — | `SWS_CANIF_00554` | unverifiable | 0.90 | — |
| — | `SWS_CANIF_00555` | unverifiable | 0.90 | — |
| — | `SWS_CANIF_00557` | unverifiable | 0.90 | — |
| — | `SWS_CANIF_00859` | unverifiable | 0.90 | — |
| — | `SWS_CANIF_00880` | unverifiable | 0.90 | — |
| — | `SWS_CANIF_00456` | unverifiable | 0.90 | — |
| — | `SWS_CANIF_00563` | missing | 1.00 | — |
| — | `SWS_CANIF_00564` | missing | 1.00 | — |
| — | `SWS_CANIF_00659` | unverifiable | 0.90 | — |
| `SRS_Can_01029` | `SWS_CANIF_00014` | implemented | 0.90 | `communication/CanIf/src/CanIf.c:1450-1495` |
| — | `SWS_CANIF_00450` | missing | 1.00 | — |
| — | `SWS_CANIF_00524` | partial | 0.80 | `communication/CanIf/src/CanIf.c:1450-1495` |
| — | `SWS_CANIF_00558` | unverifiable | 0.90 | — |
| — | `SWS_CANIF_00559` | missing | 1.00 | — |
| — | `SWS_CANIF_00560` | unverifiable | 0.90 | — |
| — | `SWS_CANIF_00821` | implemented | 1.00 | `communication/CanIf/src/CanIf.c:1853-1873` |
| — | `SWS_CANIF_00823` | missing | 1.00 | — |
| — | `SWS_CANIF_00824` | missing | 1.00 | — |
| — | `SWS_CANIF_00825` | missing | 1.00 | — |
| — | `SWS_CANIF_00826` | missing | 1.00 | — |
| — | `SWS_CANIF_00827` | missing | 1.00 | — |
| — | `SWS_CANIF_91017` | missing | 1.00 | — |
| — | `SWS_CANIF_00788` | implemented | 1.00 | `communication/CanIf/src/CanIf.c:1879-1899` |
| — | `SWS_CANIF_00958` | unverifiable | 0.90 | — |
| — | `SWS_CANIF_00959` | missing | 1.00 | — |
| — | `SWS_CANIF_00960` | unverifiable | 0.90 | — |
| — | `SWS_CANIF_00961` | missing | 1.00 | — |
| — | `SWS_CANIF_00962` | missing | 0.90 | — |
| — | `SWS_CANIF_00794` | missing | 1.00 | — |
| — | `SWS_CANIF_00795` | missing | 1.00 | — |
| — | `SWS_CANIF_00796` | unverifiable | 0.90 | — |
| — | `SWS_CANIF_00797` | missing | 1.00 | — |
| — | `SWS_CANIF_00798` | missing | 1.00 | — |
| — | `SWS_CANIF_00814` | implemented | 1.00 | `communication/CanIf/src/CanIf.c:1904-1923` |
| — | `SWS_CANIF_00800` | missing | 1.00 | — |
| — | `SWS_CANIF_00801` | missing | 1.00 | — |
| — | `SWS_CANIF_00802` | missing | 1.00 | — |
| — | `SWS_CANIF_00803` | missing | 1.00 | — |
| — | `SWS_CANIF_00804` | missing | 1.00 | — |
| — | `SWS_CANIF_00687` | implemented | 0.90 | `communication/CanIf/src/CanIf.c:585-643` |
| — | `SWS_CANIF_00689` | missing | 1.00 | — |
| — | `SWS_CANIF_00690` | missing | 0.90 | — |
| — | `SWS_CANIF_00691` | missing | 1.00 | — |
| — | `SWS_CANIF_00692` | missing | 1.00 | — |
| — | `SWS_CANIF_00693` | implemented | 1.00 | `communication/CanIf/src/CanIf.c:1626-1647` |
| — | `SWS_CANIF_00694` | partial | 0.80 | `communication/CanIf/src/CanIf.c:1626-1647` |
| — | `SWS_CANIF_00695` | missing | 1.00 | — |
| — | `SWS_CANIF_00696` | missing | 1.00 | — |
| — | `SWS_CANIF_00697` | missing | 1.00 | — |
| — | `SWS_CANIF_00698` | missing | 1.00 | — |
| `SRS_Can_02003` | `SWS_CANIF_CONSTR_00001` | unverifiable | 0.90 | — |
| `SRS_Can_02003` | `SWS_CANIF_CONSTR_00002` | unverifiable | 0.90 | — |
| `SRS_Can_02003` | `SWS_CANIF_CONSTR_00003` | unverifiable | 0.00 | — |
| `SRS_Can_02003` | `SWS_CANIF_CONSTR_00004` | unverifiable | 0.90 | — |
| `SRS_Can_02003` | `SWS_CANIF_CONSTR_00005` | unverifiable | 0.90 | — |
| `SRS_Can_02003` | `SWS_CANIF_CONSTR_00006` | unverifiable | 0.50 | — |
| `SRS_Can_02003` | `SWS_CANIF_CONSTR_00007` | unverifiable | 0.00 | — |
| `SRS_Can_02003` | `SWS_CANIF_CONSTR_00008` | unverifiable | 0.90 | — |
| `SRS_Can_02003` | `SWS_CANIF_CONSTR_00009` | unverifiable | 0.90 | — |
| `SRS_Can_02003` | `SWS_CANIF_CONSTR_00010` | missing | 1.00 | — |
| `SRS_Can_02003` | `SWS_CANIF_CONSTR_00011` | unverifiable | 0.90 | — |
| `SRS_Can_02003` | `SWS_CANIF_CONSTR_00012` | missing | 1.00 | — |
| `SRS_Can_02003` | `SWS_CANIF_CONSTR_00013` | unverifiable | 0.90 | — |
| `SRS_Can_02003` | `SWS_CANIF_CONSTR_00014` | unverifiable | 0.00 | — |
