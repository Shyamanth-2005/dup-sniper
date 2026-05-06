# Configuration Changes

## File Storage Paths Updated

### Before:
```
dedupe_log.txt          ← Root directory
.dedupe_cache.db        ← Root directory
```

### After:
```
logs/dedupe_log.txt     ← New logs folder
db/.dedupe_cache.db     ← New db folder
```

## What Changed:
✅ Log files now stored in: `logs/` folder
✅ Database cache now stored in: `db/` folder
✅ Folders created automatically if they don't exist
✅ **All functionality remains unchanged**

## Benefits:
- 📁 Better organization (logs and databases separated)
- 📊 Easier to manage and archive logs
- 🗄️ Database in dedicated folder
- 🧹 Cleaner root directory

## No Functionality Changes:
- ✅ Detection algorithms unchanged
- ✅ Performance unchanged
- ✅ Output structure unchanged
- ✅ All features work exactly the same
- ✅ DUPLICATES folder location unchanged

## How to Use:
```bash
# Works exactly as before
python delete_duplicates.py "C:/images"

# Folders will be created automatically:
# logs/dedupe_log.txt       ← Operation log
# db/.dedupe_cache.db       ← SQLite cache
# DUPLICATES/               ← Moved images
```

## Folder Structure After First Run:
```
your_project/
├── delete_duplicates.py
├── requirements.txt
├── logs/                  ← NEW: Log files here
│   └── dedupe_log.txt
├── db/                    ← NEW: Database here
│   └── .dedupe_cache.db
└── DUPLICATES/            ← Unchanged: Duplicates here
    └── images...
```
