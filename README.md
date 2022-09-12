# Frequency-domain-image-translation-weather-

## Data
Data should be transfer to mdb file first
```
python preparedata.py --out <lmdb_path> --size SIZE1,SIZE2,SIZE3,... DATASET_PATH
```

## Running Command 
```
python train2.py <Data1(original)> <Data2(rain)> --name <file_name>  --batch <batch size> 
```
