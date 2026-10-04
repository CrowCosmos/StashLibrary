import hashlib
import sys, os, json, struct, threading, time, shutil, subprocess, urllib.request, urllib.parse, urllib.error, re, zipfile, base64, hashlib, html, secrets, mimetypes, sqlite3, http.server, http.client
from pathlib import Path
from datetime import datetime, timezone
from html.parser import HTMLParser
import unicodedata
import uuid
import xml.etree.ElementTree as ET

STASHLIBRARY_VERSION='0.1.1'
STASHLIBRARY_PROTOCOL_VERSION=1
ZOTERO_HELPER_VERSION='0.1.1'
LOCAL_APPDATA=Path(os.environ.get('LOCALAPPDATA', str(Path.home())))
STASHLIBRARY_APP_ROOT=LOCAL_APPDATA/'StashLibrary'
DATA_DIR=STASHLIBRARY_APP_ROOT/'Data'
LEGACY_APP_ROOT=LOCAL_APPDATA/('Lo'+'cero')
LEGACY_DATA_DIR=LEGACY_APP_ROOT/'Data'
LEGACY_APPDIR=LOCAL_APPDATA/'LocalFileBookmarks'
DATA_DIR.mkdir(parents=True, exist_ok=True)
LEGACY_CONFIG=LEGACY_APPDIR/'config.json'
CONFIG=DATA_DIR/'config.json'
# StashLibrary keeps configuration in the durable LocalAppData data directory.
# Copy rather than delete the legacy file so a downgrade cannot strand an existing setup.
if not CONFIG.exists():
    for legacy_config in (LEGACY_DATA_DIR/'config.json',LEGACY_CONFIG):
        if legacy_config.exists():
            try:shutil.copy2(legacy_config,CONFIG);break
            except Exception:pass
APPDIR=DATA_DIR
lock=threading.Lock(); last_sig=None
HISTORY_LIMIT=30
SYSTEM_DIR_NAME='.local-bookmarks-data'
UNIFIED_DATA_DIR_NAME='.stashlibrary-data'
LEGACY_UNIFIED_DATA_DIR_NAME='.stashlibrary-data'
DATABASE_NAME='stashlibrary.sqlite3'
DATABASE_SCHEMA_VERSION=2
DB_LOCK=threading.RLock()

# Provider-neutral cloud-folder mirroring. StashLibrary never opens its live SQLite
# catalogue from a cloud-managed folder; only a verified snapshot is published.
CLOUD_MIRROR_DIR='archive'
CLOUD_META_DIR='.stashlibrary-cloud'
CLOUD_DB_NAME='stashlibrary.sqlite3'
CLOUD_STATE_NAME='sync.json'
CLOUD_SYNC_LOCK=threading.RLock()
CLOUD_SYNC_TIMER_LOCK=threading.Lock()
CLOUD_SYNC_TIMER=None
CLOUD_SYNC_DEBOUNCE_SECONDS=8.0

# v0.10.169: optional direct WebDAV cloud backup. The local StashLibrary folder is
# always the live library. WebDAV is a separate, incremental backup target.
WEBDAV_BACKUP_LOCK=threading.Lock()
WEBDAV_TIMER_LOCK=threading.Lock()
WEBDAV_TIMER=None
WEBDAV_DEBOUNCE_SECONDS=20.0
WEBDAV_REMOTE_ROOT='StashLibrary'
WEBDAV_REMOTE_FILES='files'
WEBDAV_STATE_NAME='backup-state.json'
WEBDAV_DB_NAME='stashlibrary.sqlite3'
WEBDAV_FORMAT='StashLibrary WebDAV cloud backup'
WEBDAV_FORMAT_VERSION=2


def _native_helper_update_watchdog():
    """Exit this native host when the installer activates another side-by-side build.

    Firefox notices the native-messaging port close and can reconnect to the new
    registry target without closing the browser or any tabs. Source/dev launches
    are unaffected because ACTIVE_HOST only exists under the installed helper root.
    """
    try:
        here=Path(__file__).resolve().parent
        helper_root=here.parent
        marker=helper_root/'ACTIVE_HOST'
        time.sleep(1.0)
        while True:
            try:
                if marker.exists():
                    active=marker.read_text(encoding='utf-8').strip()
                    if active:
                        active_path=Path(active).resolve()
                        if os.path.normcase(str(active_path))!=os.path.normcase(str(here)):
                            os._exit(0)
            except Exception:
                pass
            time.sleep(0.75)
    except Exception:
        pass

threading.Thread(target=_native_helper_update_watchdog,name='StashLibraryHelperUpdateWatchdog',daemon=True).start()

RESERVED={'bookmarks','backups'}

def read_config():
    try:return json.loads(CONFIG.read_text(encoding='utf-8'))
    except:return {}
def write_config(c):CONFIG.write_text(json.dumps(c,indent=2),encoding='utf-8')

def default_library_path():
    """Return the drive-root StashLibrary folder for the current Windows user."""
    home_drive=Path.home().drive
    system_drive=Path(os.environ.get('SystemDrive','')).drive
    drive=home_drive or system_drive
    return Path(drive+'\\')/'StashLibrary' if drive else Path.home()/'StashLibrary'

def ensure_default_library():
    """Create and configure the default library for a fresh installation."""
    c=read_config()
    if str(c.get('bookmarks_path') or '').strip():return
    target=default_library_path()
    target.mkdir(parents=True,exist_ok=True)
    c['bookmarks_path']=str(target)
    c['cloud_sync_path']=str(target)
    c['storage_sync_mode']='local'
    c['internal_data_layout']='library-local-v3'
    c['internal_data_path']=str(target/UNIFIED_DATA_DIR_NAME)
    c['storage_root_layout']='self-contained-v1'
    c['folder_config_version']=7
    write_config(c)

def bookmarks_path():
    p=read_config().get('bookmarks_path')
    return Path(p) if p else None

def _is_explicit_library_root(root,c=None):
    """Return True when *root* was deliberately selected by the user.

    Selecting a storage folder means opening that library, not migrating the
    previously-open library into it. This marker also protects an intentionally
    empty selected library from old upgrade-repair code.
    """
    if not root:return False
    c=dict(c or read_config())
    selected=str(c.get('explicit_library_root') or '').strip()
    return bool(selected and _same_fs_path(root,selected))

def uses_separated_internal_data():
    return read_config().get('internal_data_layout') in {'localappdata-v1','library-local-v2','library-local-v3'}

def internal_data_dir(root=None, create=True):
    c=read_config()
    layout=c.get('internal_data_layout')
    if layout=='localappdata-v1':
        configured=str(c.get('internal_data_path') or '').strip()
        p=Path(configured) if configured else DATA_DIR
    else:
        b=Path(root) if root else bookmarks_path()
        dirname=UNIFIED_DATA_DIR_NAME if layout in {'library-local-v2','library-local-v3'} else SYSTEM_DIR_NAME
        p=(b/dirname) if b else None
    if p and create:p.mkdir(parents=True,exist_ok=True)
    return p

def undo_root():
    return internal_data_dir()

def undo_dir():
    p=undo_root()
    if p:
        p=p/'undo-store';p.mkdir(parents=True,exist_ok=True)
    return p

def history_file():
    p=undo_root()
    if p:
        p.mkdir(parents=True,exist_ok=True)
        return p/'history.json'
    return None

ROOT_PATH_TOKEN='@ROOT/'
UNDO_PATH_TOKEN='@UNDO/'
_HISTORY_PATH_KEYS={'path','stored','old','new','parent','dest_parent'}

def _resolve_history_path(value):
    """Resolve a path saved in undo history against the library's current location."""
    if isinstance(value,Path): return value
    s=str(value or '')
    if s.startswith(UNDO_PATH_TOKEN):
        base=undo_dir()
        return (base/Path(s[len(UNDO_PATH_TOKEN):])) if base else Path('__missing_undo_store__')
    if s.startswith(ROOT_PATH_TOKEN):
        root=bookmarks_path()
        return (root/Path(s[len(ROOT_PATH_TOKEN):])) if root else Path('__missing_bookmarks_root__')
    # Backwards compatibility with history written by older StashLibrary versions.
    return Path(s)

def _encode_history_path(value):
    """Store library/undo paths relative to the selected StashLibrary folder."""
    if value is None:return value
    s=str(value)
    if s.startswith((ROOT_PATH_TOKEN,UNDO_PATH_TOKEN)):return s
    p=Path(s)
    root=bookmarks_path()
    u=undo_dir()
    if u:
        try:
            rel=p.resolve(strict=False).relative_to(u.resolve(strict=False))
            return UNDO_PATH_TOKEN+rel.as_posix()
        except Exception:pass
    if root:
        try:
            rel=p.resolve(strict=False).relative_to(root.resolve(strict=False))
            return ROOT_PATH_TOKEN+rel.as_posix()
        except Exception:pass
    return s

def _legacy_root_from_action(action):
    """Infer the old bookmarks root from an older absolute undo-store path."""
    values=[action.get(k) for k in _HISTORY_PATH_KEYS if action.get(k)]
    for value in values:
        s=str(value)
        if s.startswith((ROOT_PATH_TOKEN,UNDO_PATH_TOKEN)):continue
        try:
            p=Path(s)
            parts=list(p.parts)
            if SYSTEM_DIR_NAME in parts or UNIFIED_DATA_DIR_NAME in parts or LEGACY_UNIFIED_DATA_DIR_NAME in parts:
                if SYSTEM_DIR_NAME in parts:idx=parts.index(SYSTEM_DIR_NAME)
                elif UNIFIED_DATA_DIR_NAME in parts:idx=parts.index(UNIFIED_DATA_DIR_NAME)
                else:idx=parts.index(LEGACY_UNIFIED_DATA_DIR_NAME)
                if idx>0:return Path(*parts[:idx])
        except Exception:pass
    return None

def _portable_action(action):
    """Convert old absolute history entries to portable relative tokens where possible."""
    a=dict(action or {})
    if a.get('type')=='group':
        a['actions']=[_portable_action(x) for x in list(a.get('actions') or [])]
        return a
    old_root=_legacy_root_from_action(a)
    current_root=bookmarks_path()
    for key in _HISTORY_PATH_KEYS:
        value=a.get(key)
        if not value:continue
        s=str(value)
        if s.startswith((ROOT_PATH_TOKEN,UNDO_PATH_TOKEN)):continue
        p=Path(s)

        # Recovery payloads can always be made relative from their undo-store suffix.
        try:
            parts=list(p.parts)
            if 'undo-store' in parts:
                idx=parts.index('undo-store')
                rel=Path(*parts[idx+1:])
                a[key]=UNDO_PATH_TOKEN+rel.as_posix()
                continue
        except Exception:pass

        # If we can infer the library's previous root, remap the same relative suffix.
        if old_root:
            try:
                rel=p.relative_to(old_root)
                a[key]=ROOT_PATH_TOKEN+rel.as_posix()
                continue
            except Exception:pass

        # Or encode paths that are already under the current library root.
        a[key]=_encode_history_path(p)
    return a

def _same_fs_path(a,b):
    try:
        return os.path.normcase(str(Path(a).resolve(strict=False)))==os.path.normcase(str(Path(b).resolve(strict=False)))
    except Exception:
        return os.path.normcase(str(a))==os.path.normcase(str(b))

def _files_identical(a,b):
    a=Path(a);b=Path(b)
    try:
        if a.stat().st_size!=b.stat().st_size:return False
        h1=hashlib.sha256();h2=hashlib.sha256()
        with a.open('rb') as f:
            for chunk in iter(lambda:f.read(1024*1024),b''):h1.update(chunk)
        with b.open('rb') as f:
            for chunk in iter(lambda:f.read(1024*1024),b''):h2.update(chunk)
        return h1.digest()==h2.digest()
    except Exception:
        return False

def _safe_copy_contents(source,target,skip_top_names=None):
    """Copy a directory tree without overwriting a different existing file.

    This helper is intentionally copy-only. Storage migrations must never make
    the source copy disappear; cleanup can happen only after the user has a
    verified backup and in a later, explicit operation.
    """
    source=Path(source);target=Path(target)
    if not source.exists() or not source.is_dir():return 0
    target.mkdir(parents=True,exist_ok=True)
    skip=set(skip_top_names or [])
    copied=0
    for src in source.rglob('*'):
        rel=src.relative_to(source)
        if rel.parts and rel.parts[0] in skip:continue
        dst=target/rel
        if src.is_dir():
            dst.mkdir(parents=True,exist_ok=True);continue
        if not src.is_file():continue
        dst.parent.mkdir(parents=True,exist_ok=True)
        if dst.exists():
            if not dst.is_file() or not _files_identical(src,dst):
                raise RuntimeError(f'Storage migration stopped because a different file already exists at {dst}.')
        else:
            shutil.copy2(src,dst);copied+=1
        if not dst.exists() or not _files_identical(src,dst):
            raise RuntimeError(f'Could not verify copied StashLibrary file: {src.name}')
    return copied

def migrate_internal_data_to_localappdata():
    """Copy private catalogue state into LocalAppData without touching archives.

    v0.10.140 mistakenly copied the legacy ``archive`` directory into AppData
    and then removed the old private directory. v0.10.141 is deliberately
    conservative: archive/view payloads are excluded, private data is copied
    and verified, and the legacy source is *never deleted automatically*.
    """
    c=read_config()
    root_value=str(c.get('bookmarks_path') or '').strip()
    if not root_value or c.get('internal_data_layout')=='localappdata-v1':
        return
    root=Path(root_value)
    layout=c.get('internal_data_layout')
    legacy=root/LEGACY_UNIFIED_DATA_DIR_NAME if layout=='library-local-v2' else root/SYSTEM_DIR_NAME
    DATA_DIR.mkdir(parents=True,exist_ok=True)

    if legacy.exists() and legacy.is_dir():
        # Never copy canonical archive files or the derived Explorer view into
        # AppData. Everything else here is catalogue/recovery/undo state.
        for src in legacy.iterdir():
            if src.name in {'archive','view'}:
                continue
            dst=DATA_DIR/src.name
            if src.is_dir():
                shutil.copytree(src,dst,dirs_exist_ok=True)
            elif src.is_file():
                shutil.copy2(src,dst)

        db=DATA_DIR/DATABASE_NAME
        if db.exists():
            with sqlite3.connect(str(db)) as conn:
                row=conn.execute('PRAGMA quick_check').fetchone()
                if not row or str(row[0]).lower()!='ok':
                    raise RuntimeError('Could not verify the StashLibrary database after copying it to AppData.')

    c=read_config()
    c['internal_data_layout']='localappdata-v1'
    c['internal_data_path']=str(DATA_DIR)
    try:c['folder_config_version']=max(6,int(c.get('folder_config_version') or 0))
    except Exception:c['folder_config_version']=6
    c['internal_data_migrated_at']=datetime.now(timezone.utc).isoformat()
    c['internal_data_source_preserved']=True
    write_config(c)


def migrate_storage_root_to_userprofile():
    """Safely standardise the visible archive at the drive-root ``StashLibrary`` folder.

    Existing files are copied and byte-verified before the configured root is
    switched. Sources are never moved or deleted. This also repairs the
    v0.10.140 regression by copying any accidentally-created
    ``%LOCALAPPDATA%\\StashLibrary\\Data\\archive`` payload back into the visible
    archive root while leaving that AppData copy untouched as a safety copy.
    """
    c=read_config()
    current_value=str(c.get('bookmarks_path') or '').strip()
    if not current_value:return
    target=default_library_path()
    current=Path(current_value)
    migration_marker='drive-root-stashlibrary-v2'

    # A new installer already points here. Stamp the layout marker and return.
    if _same_fs_path(current,target):
        target.mkdir(parents=True,exist_ok=True)
        if c.get('storage_root_brand_migration')!=migration_marker:
            c['storage_root_brand_migration']=migration_marker
            write_config(c)
        return

    # A folder that still carries a historical product name is migrated even if
    # it was selected in Settings. The copy is byte-verified and the source is
    # preserved, so existing users consistently end up at <drive>\StashLibrary.
    # Genuinely custom folder names remain authoritative.
    if current.name.casefold() not in {'stashlibrary','stashreadz'}:return

    # Avoid retrying a successfully completed migration forever.
    if c.get('storage_root_brand_migration')==migration_marker:return

    sources=[]
    # Current separated-layout files, if any, live directly in the old root.
    if current.exists() and current.is_dir():
        sources.append((current,{SYSTEM_DIR_NAME,UNIFIED_DATA_DIR_NAME,LEGACY_UNIFIED_DATA_DIR_NAME}))
        # Pre-separated flat layouts kept their payload here.
        legacy_archive=current/SYSTEM_DIR_NAME/'archive'
        if legacy_archive.exists():sources.append((legacy_archive,set()))

    # v0.10.140 regression recovery source. Copy only; never remove it here.
    misplaced=DATA_DIR/'archive'
    if misplaced.exists() and misplaced.is_dir():sources.append((misplaced,set()))

    try:
        target.mkdir(parents=True,exist_ok=True)
        copied=0
        seen=set()
        for source,skip in sources:
            key=os.path.normcase(str(source.resolve(strict=False)))
            if key in seen or _same_fs_path(source,target):continue
            seen.add(key)
            copied+=_safe_copy_contents(source,target,skip)
    except Exception as e:
        c=read_config()
        c['storage_root_migration_error']=str(e)
        c['storage_root_migration_error_at']=datetime.now(timezone.utc).isoformat()
        write_config(c)
        return

    # Switch only after every source file that we attempted to copy verified.
    c=read_config()
    c['previous_bookmarks_path']=current_value
    c['bookmarks_path']=str(target)
    c['storage_root_layout']='self-contained-v1'
    c['storage_root_brand_migration']=migration_marker
    c['storage_root_migrated_at']=datetime.now(timezone.utc).isoformat()
    c['storage_root_source_preserved']=True
    c['storage_root_files_copied']=copied
    c.pop('storage_root_migration_error',None)
    c.pop('storage_root_migration_error_at',None)
    write_config(c)

def _copy_internal_data_safely(source,target):
    """Copy StashLibrary catalogue/recovery/history state into a library-local data folder.

    The SQLite catalogue is copied with SQLite's backup API rather than as a raw
    file so a WAL-mode database is consistent. Other recovery/undo files are
    copied without deleting the source; migration cleanup is deliberately left
    to the user after they have verified the new library.
    """
    source=Path(source);target=Path(target);target.mkdir(parents=True,exist_ok=True)
    if not source.exists() or _same_fs_path(source,target):return 0
    copied=0
    db_src=source/DATABASE_NAME;db_dst=target/DATABASE_NAME
    skip={DATABASE_NAME,DATABASE_NAME+'-wal',DATABASE_NAME+'-shm','config.json','archive','view'}
    for src in source.iterdir():
        if src.name in skip:continue
        dst=target/src.name
        if src.is_dir():
            shutil.copytree(src,dst,dirs_exist_ok=True);copied+=1
        elif src.is_file():
            shutil.copy2(src,dst);copied+=1
    if db_src.exists():
        tmp=db_dst.with_name(db_dst.name+f'.migrate-{uuid.uuid4().hex}.tmp')
        try:
            with DB_LOCK:
                src_conn=sqlite3.connect(str(db_src),timeout=20)
                dst_conn=sqlite3.connect(str(tmp),timeout=20)
                try:
                    src_conn.backup(dst_conn);dst_conn.commit()
                finally:
                    dst_conn.close();src_conn.close()
            with sqlite3.connect(f'file:{tmp.as_posix()}?mode=ro&immutable=1',uri=True) as check:
                row=check.execute('PRAGMA quick_check').fetchone()
                if not row or str(row[0]).lower()!='ok':
                    raise RuntimeError('Could not verify the StashLibrary database after copying it into the StashLibrary folder.')
            _install_verified_catalogue_copy(tmp,db_dst);copied+=1
        finally:
            try:tmp.unlink(missing_ok=True)
            except Exception:pass
    return copied


def _probe_catalogue_file(path):
    """Return basic catalogue stats for a valid StashLibrary SQLite file, else None.

    This intentionally does not call the normal database helpers because it is
    used while repairing older storage layouts before the active catalogue path
    is switched.
    """
    path=Path(path)
    if not path.is_file():return None
    try:
        with sqlite3.connect(f'file:{path.as_posix()}?mode=ro',uri=True,timeout=20) as conn:
            row=conn.execute('PRAGMA quick_check').fetchone()
            if not row or str(row[0]).lower()!='ok':return None
            tables={str(r[0]) for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            if not {'nodes','files','catalogue_meta'}.issubset(tables):return None
            nodes=int(conn.execute("SELECT COUNT(*) FROM nodes WHERE kind!='root'").fetchone()[0] or 0)
            files=int(conn.execute('SELECT COUNT(*) FROM files').fetchone()[0] or 0)
            bookmarks=int(conn.execute("SELECT COUNT(*) FROM nodes WHERE kind='bookmark'").fetchone()[0] or 0)
            lr=conn.execute("SELECT value FROM catalogue_meta WHERE key='library_id' LIMIT 1").fetchone()
            library_id=str(lr[0] if lr else '').strip()
        return {'path':path,'nodes':nodes,'files':files,'bookmarks':bookmarks,'libraryId':library_id,'mtimeNs':int(path.stat().st_mtime_ns)}
    except Exception:
        return None


def _catalogue_stats_match(a,b):
    if not a or not b:return False
    return all(int(a.get(k) or 0)==int(b.get(k) or 0) for k in ('nodes','files','bookmarks')) and str(a.get('libraryId') or '')==str(b.get('libraryId') or '')


def _install_verified_catalogue_copy(source_db,target_db):
    """Install a verified SQLite catalogue without relying on replacing an open file.

    Windows refuses os.replace() when another StashLibrary request (or a SQLite viewer)
    still has the destination file open.  If the destination already exists, use
    SQLite's backup API to update it in place under DB_LOCK.  That works with normal
    SQLite readers and is transactional at the database level.  A rename is kept as
    the fallback for a missing/non-SQLite destination.
    """
    source_db=Path(source_db);target_db=Path(target_db);target_db.parent.mkdir(parents=True,exist_ok=True)
    source_info=_probe_catalogue_file(source_db)
    if not source_info:raise RuntimeError('StashLibrary could not verify the replacement catalogue before installing it.')
    inplace_error=None
    with DB_LOCK:
        if target_db.exists():
            try:
                src=sqlite3.connect(f'file:{source_db.as_posix()}?mode=ro',uri=True,timeout=30)
                dst=sqlite3.connect(str(target_db),timeout=30)
                try:
                    dst.execute('PRAGMA busy_timeout=30000')
                    src.backup(dst,pages=256,sleep=0.05)
                    dst.commit()
                    try:
                        dst.execute('PRAGMA wal_checkpoint(TRUNCATE)')
                        dst.commit()
                    except Exception:pass
                finally:
                    dst.close();src.close()
                installed=_probe_catalogue_file(target_db)
                if not _catalogue_stats_match(source_info,installed):
                    raise RuntimeError('The repaired catalogue did not match the verified source catalogue.')
                return installed
            except Exception as e:
                inplace_error=e
        # Missing or unusable destination: an atomic rename is still preferable.
        for suffix in ('-wal','-shm'):
            try:Path(str(target_db)+suffix).unlink(missing_ok=True)
            except Exception:pass
        try:
            os.replace(source_db,target_db)
        except OSError as e:
            winerror=getattr(e,'winerror',None)
            if winerror==32 or isinstance(e,PermissionError):
                detail=f' ({inplace_error})' if inplace_error else ''
                raise RuntimeError(
                    'StashLibrary could not finish repairing the local catalogue because the SQLite file is still open. '
                    'Close any SQLite/database viewer that has stashlibrary.sqlite3 open, then try Back Up Now again.'+detail
                ) from e
            raise
        installed=_probe_catalogue_file(target_db)
        if not _catalogue_stats_match(source_info,installed):
            raise RuntimeError('StashLibrary installed the repaired catalogue but could not verify its contents.')
        return installed


def _self_contained_catalogue_candidates(root,c=None):
    """Find older catalogue locations that may safely repair .stashlibrary-data.

    v0.10.164-170 deliberately kept the live catalogue in LocalAppData, while
    still older builds used .local-bookmarks-data inside the library.  A user
    can therefore legitimately have a healthy catalogue in one of several old
    locations after upgrading or changing the Local Storage folder.
    """
    root=Path(root);c=dict(c or read_config());target=root/UNIFIED_DATA_DIR_NAME
    dirs=[]
    def add(value):
        if not value:return
        try:q=Path(value)
        except Exception:return
        if _same_fs_path(q,target):return
        key=os.path.normcase(str(q.resolve(strict=False)))
        if any(k==key for k,_ in dirs):return
        dirs.append((key,q))
    configured=str(c.get('internal_data_path') or '').strip()
    if configured:add(configured)
    add(DATA_DIR)
    add(root/SYSTEM_DIR_NAME)
    add(root/LEGACY_UNIFIED_DATA_DIR_NAME)
    previous=str(c.get('previous_bookmarks_path') or '').strip()
    if previous:
        prev=Path(previous);add(prev/UNIFIED_DATA_DIR_NAME);add(prev/LEGACY_UNIFIED_DATA_DIR_NAME);add(prev/SYSTEM_DIR_NAME)
    add(LEGACY_APPDIR);add(LEGACY_APPDIR/SYSTEM_DIR_NAME);add(DATA_DIR/SYSTEM_DIR_NAME)
    out=[]
    for _,d in dirs:
        info=_probe_catalogue_file(d/DATABASE_NAME)
        if info:
            info['dataDir']=d;out.append(info)
    return out


def _copy_catalogue_for_self_contained_repair(source_db,target_db):
    """Copy a catalogue with SQLite backup semantics and verify the result."""
    source_db=Path(source_db);target_db=Path(target_db);target_db.parent.mkdir(parents=True,exist_ok=True)
    tmp=target_db.with_name(target_db.name+f'.repair-{uuid.uuid4().hex}.tmp')
    try:
        src=sqlite3.connect(str(source_db),timeout=20);dst=sqlite3.connect(str(tmp),timeout=20)
        try:
            src.backup(dst);dst.commit()
        finally:
            dst.close();src.close()
        info=_probe_catalogue_file(tmp)
        if not info:raise RuntimeError('Could not verify the repaired StashLibrary catalogue.')
        if target_db.exists():
            old=_probe_catalogue_file(target_db)
            stamp=datetime.now().strftime('%Y%m%d-%H%M%S')
            preserved=target_db.with_name(f'stashlibrary-before-self-contained-repair-{stamp}.sqlite3')
            try:shutil.copy2(target_db,preserved)
            except Exception:pass
        return _install_verified_catalogue_copy(tmp,target_db)
    finally:
        try:tmp.unlink(missing_ok=True)
        except Exception:pass


def repair_self_contained_library_data(root=None):
    """Repair a missing/accidentally empty .stashlibrary-data catalogue when possible.

    The repair is deliberately conservative: a populated modern catalogue is
    never replaced.  We only repair when the modern catalogue is missing,
    invalid, or empty while a verified older catalogue contains real records.
    No source catalogue is deleted.
    """
    root=Path(root) if root else bookmarks_path()
    if not root:return {'repaired':False,'reason':'no-library'}
    root.mkdir(parents=True,exist_ok=True)
    target_dir=root/UNIFIED_DATA_DIR_NAME;target_dir.mkdir(parents=True,exist_ok=True);target_db=target_dir/DATABASE_NAME
    current=_probe_catalogue_file(target_db)
    # A selected StashLibrary folder may already be authoritative while its
    # catalogue still uses the former branded directory name. This is an
    # in-place namespace migration, not an attempt to resurrect another
    # library, so it is safe even for explicitly selected roots.
    legacy_dir=root/LEGACY_UNIFIED_DATA_DIR_NAME
    legacy_current=_probe_catalogue_file(legacy_dir/DATABASE_NAME)
    if legacy_current and (
        current is None or (
            int(current.get('nodes') or 0)==0 and
            (int(legacy_current.get('nodes') or 0)>0 or int(legacy_current.get('files') or 0)>0)
        )
    ):
        copied=_copy_internal_data_safely(legacy_dir,target_dir)
        installed=_probe_catalogue_file(target_db)
        if not installed:raise RuntimeError('Could not verify the catalogue after migrating it to .stashlibrary-data.')
        c=read_config();c['internal_data_layout']='library-local-v3';c['internal_data_path']=str(target_dir);c['storage_root_layout']='self-contained-v1'
        c['stashlibrary_data_migrated_at']=datetime.now(timezone.utc).isoformat();c['stashlibrary_data_source_preserved']=True;c['stashlibrary_data_files_copied']=copied
        write_config(c)
        return {'repaired':True,'source':str(legacy_dir/DATABASE_NAME),'nodes':int(installed.get('nodes') or 0),'files':int(installed.get('files') or 0)}
    # An explicitly selected folder is authoritative even when it is empty.
    # Never resurrect the previous library into it automatically.
    if _is_explicit_library_root(root):
        if current:
            return {'repaired':False,'reason':'explicit-library','nodes':int(current.get('nodes') or 0),'files':int(current.get('files') or 0)}
        return {'repaired':False,'reason':'explicit-library-empty'}
    candidates=_self_contained_catalogue_candidates(root)
    populated=[x for x in candidates if int(x.get('nodes') or 0)>0 or int(x.get('files') or 0)>0]
    source=None
    if current is None:
        # Prefer a populated source; otherwise the newest valid catalogue is
        # still better than creating an unverified empty shell.
        pool=populated or candidates
        if pool:source=max(pool,key=lambda x:(int(x.get('nodes') or 0)>0,int(x.get('nodes') or 0),int(x.get('files') or 0),int(x.get('mtimeNs') or 0)))
    elif int(current.get('nodes') or 0)==0 and populated:
        # This specifically repairs the v0.10.171-181 failure mode where the
        # config said library-local-v3 but the real catalogue was still in the
        # previous/AppData location, causing SQLite to create an empty shell.
        source=max(populated,key=lambda x:(int(x.get('nodes') or 0),int(x.get('files') or 0),int(x.get('mtimeNs') or 0)))
    if source:
        repaired=_copy_catalogue_for_self_contained_repair(source['path'],target_db)
        # Bring across recovery/history files only when the modern folder does
        # not already have them.  Never overwrite newer local recovery state.
        srcdir=Path(source.get('dataDir') or source['path'].parent)
        if srcdir.exists():
            for item in srcdir.iterdir():
                if item.name in {DATABASE_NAME,DATABASE_NAME+'-wal',DATABASE_NAME+'-shm','config.json','archive','view'}:continue
                dst=target_dir/item.name
                if dst.exists():continue
                try:
                    if item.is_dir():shutil.copytree(item,dst)
                    elif item.is_file():shutil.copy2(item,dst)
                except Exception:pass
        c=read_config();c['internal_data_layout']='library-local-v3';c['internal_data_path']=str(target_dir);c['storage_root_layout']='self-contained-v1'
        c['self_contained_repaired_at']=datetime.now(timezone.utc).isoformat();c['self_contained_repair_source']=str(source['path'])
        c['self_contained_repair_nodes']=int((repaired or {}).get('nodes') or 0);c['self_contained_repair_files']=int((repaired or {}).get('files') or 0)
        c.pop('library_data_migration_error',None);c.pop('library_data_migration_error_at',None);write_config(c)
        return {'repaired':True,'source':str(source['path']),'nodes':int((repaired or {}).get('nodes') or 0),'files':int((repaired or {}).get('files') or 0)}
    if current:
        c=read_config()
        if c.get('internal_data_layout')!='library-local-v3' or str(c.get('internal_data_path') or '')!=str(target_dir):
            c['internal_data_layout']='library-local-v3';c['internal_data_path']=str(target_dir);c['storage_root_layout']='self-contained-v1';write_config(c)
        return {'repaired':False,'reason':'already-valid','nodes':int(current.get('nodes') or 0),'files':int(current.get('files') or 0)}
    return {'repaired':False,'reason':'no-valid-catalogue-found'}


def migrate_internal_data_to_library():
    """Make the selected StashLibrary folder self-contained.

    v0.10.171 stores catalogue, recovery and undo/history data together in
    ``<StashLibrary folder>\\.stashlibrary-data``. Machine-specific configuration and cloud
    credentials remain in LocalAppData so copying the StashLibrary folder does not
    copy account secrets.
    """
    c=read_config();root_value=str(c.get('bookmarks_path') or '').strip()
    if not root_value:return
    root=Path(root_value);target=root/UNIFIED_DATA_DIR_NAME;target.mkdir(parents=True,exist_ok=True)
    layout=str(c.get('internal_data_layout') or '')
    # A folder deliberately chosen in Settings is an independent library.
    # If it is empty, leave it empty; the normal database open path will create
    # a fresh catalogue instead of copying/repairing data from the old root.
    if _is_explicit_library_root(root,c):
        changed=False
        if c.get('internal_data_layout')!='library-local-v3':c['internal_data_layout']='library-local-v3';changed=True
        if str(c.get('internal_data_path') or '')!=str(target):c['internal_data_path']=str(target);changed=True
        if c.get('storage_root_layout')!='self-contained-v1':c['storage_root_layout']='self-contained-v1';changed=True
        if changed:write_config(c)
        return
    # Do not trust the layout marker by itself.  Some v0.10.171-181 upgrades
    # could stamp library-local-v3 before the real catalogue reached the folder.
    # Repair from a verified older catalogue if the modern one is missing/empty.
    try:repair_self_contained_library_data(root)
    except Exception as e:
        c=read_config();c['library_data_migration_error']=str(e);c['library_data_migration_error_at']=datetime.now(timezone.utc).isoformat();write_config(c)
    if _probe_catalogue_file(target/DATABASE_NAME):return
    if layout=='library-local-v2':
        # Same physical location, just stamp the new self-contained layout marker.
        c['internal_data_layout']='library-local-v3';c['internal_data_path']=str(target)
        c['library_data_migrated_at']=datetime.now(timezone.utc).isoformat();write_config(c);return
    if layout=='localappdata-v1':
        configured=str(c.get('internal_data_path') or '').strip();source=Path(configured) if configured else DATA_DIR
    else:
        previous=str(c.get('previous_bookmarks_path') or '').strip();source_root=Path(previous) if previous else root
        source=source_root/SYSTEM_DIR_NAME
    try:
        copied=_copy_internal_data_safely(source,target)
        db=target/DATABASE_NAME
        if db.exists():
            with sqlite3.connect(f'file:{db.as_posix()}?mode=ro&immutable=1',uri=True) as conn:
                row=conn.execute('PRAGMA quick_check').fetchone()
                if not row or str(row[0]).lower()!='ok':raise RuntimeError('Could not verify the StashLibrary database in the local StashLibrary folder.')
    except Exception as e:
        c=read_config();c['library_data_migration_error']=str(e);c['library_data_migration_error_at']=datetime.now(timezone.utc).isoformat();write_config(c);return
    c=read_config();c['internal_data_layout']='library-local-v3';c['internal_data_path']=str(target)
    c['library_data_migrated_at']=datetime.now(timezone.utc).isoformat();c['library_data_source_preserved']=True;c['library_data_files_copied']=copied
    c.pop('library_data_migration_error',None);c.pop('library_data_migration_error_at',None);write_config(c)



def migrate_v163_cloud_mirror_to_single_folder():
    """Adopt the v0.10.163 mirror folder as the one visible StashLibrary folder.

    v0.10.163 kept a live storage root plus a second cloud mirror. v0.10.164
    removes that duplication: archived files live directly in the selected
    synced folder, while the live SQLite catalogue stays in LocalAppData.
    Sources are copied and verified before the configured root changes; the old
    live storage folder is deliberately preserved as a recovery copy.
    """
    c=read_config()
    # This migration belongs only to the retired desktop-folder sync model.
    # Never force a modern self-contained library back into LocalAppData.
    if c.get('cloud_sync_model') not in {'provider-neutral-v1','single-folder-v1'} and not c.get('cloud_sync_enabled'):
        return
    current_value=str(c.get('bookmarks_path') or '').strip()
    mirror_value=str(c.get('cloud_sync_path') or '').strip()
    if not current_value:
        return

    # v0.10.164 always keeps active catalogue/undo state in LocalAppData.
    c['internal_data_layout']='localappdata-v1'
    c['internal_data_path']=str(DATA_DIR)
    c['cloud_sync_enabled']=True
    c['cloud_sync_model']='single-folder-v1'

    if not mirror_value:
        # Existing users who never configured cloud sync simply keep their
        # current StashLibrary folder. It can later be moved with Choose Folder.
        c['cloud_sync_path']=current_value
        write_config(c)
        return

    current=Path(current_value)
    target=Path(mirror_value)
    if _same_fs_path(current,target):
        c['cloud_sync_path']=str(target)
        c['bookmarks_path']=str(target)
        c['storage_root_layout']='single-folder-v1'
        write_config(c)
        return

    # If a genuinely newer mirror from another computer is present, do not
    # silently overwrite/adopt it during an upgrade. The user can choose that
    # folder and Restore From Cloud deliberately.
    state={}
    state_path=target/CLOUD_META_DIR/CLOUD_STATE_NAME
    try:
        state=json.loads(state_path.read_text(encoding='utf-8')) if state_path.exists() else {}
    except Exception:
        state={}
    remote_rev=int(state.get('revision') or 0) if isinstance(state,dict) else 0
    last_seen=int(c.get('cloud_last_seen_revision') or 0)
    local_device=str(c.get('cloud_device_id') or '')
    remote_device=str(state.get('deviceId') or '') if isinstance(state,dict) else ''
    remote_newer=bool(remote_device and local_device and remote_device!=local_device and remote_rev>last_seen)

    try:
        target.mkdir(parents=True,exist_ok=True)
        # If this computer is the current/same revision, migrate its visible
        # archive into the new root. If another computer has a newer cloud
        # revision, preserve this computer's old root untouched and adopt the
        # cloud folder only; preflight then blocks edits until Restore From Cloud.
        if not remote_newer:
            _safe_copy_contents(current,target,{SYSTEM_DIR_NAME,UNIFIED_DATA_DIR_NAME,LEGACY_UNIFIED_DATA_DIR_NAME,CLOUD_META_DIR})

        # v0.10.163 put archive files under mirror/archive/. Flatten that old
        # mirror only when every file can be copied/verified without conflict.
        legacy_archive=target/CLOUD_MIRROR_DIR
        if legacy_archive.exists() and legacy_archive.is_dir():
            _safe_copy_contents(legacy_archive,target,set())
            verified=True
            for src in legacy_archive.rglob('*'):
                if src.is_file():
                    dst=target/src.relative_to(legacy_archive)
                    if not dst.is_file() or not _files_identical(src,dst):
                        verified=False;break
            if verified:
                shutil.rmtree(legacy_archive)

        c=read_config()
        c['previous_bookmarks_path']=current_value
        c['bookmarks_path']=str(target)
        c['cloud_sync_path']=str(target)
        c['internal_data_layout']='localappdata-v1'
        c['internal_data_path']=str(DATA_DIR)
        c['cloud_sync_enabled']=True
        c['cloud_sync_model']='single-folder-v1'
        c['storage_root_layout']='single-folder-v1'
        c['single_folder_migrated_at']=datetime.now(timezone.utc).isoformat()
        c['single_folder_source_preserved']=True
        if remote_newer:c['cloud_single_folder_restore_required']=True
        else:c.pop('cloud_single_folder_restore_required',None)
        c.pop('cloud_single_folder_migration_pending',None)
        write_config(c)
    except Exception as e:
        c=read_config();c['cloud_single_folder_migration_error']=str(e);write_config(c)


# Keep legacy desktop-cloud-folder migration compatibility, then reunify
# catalogue/recovery/undo state into the selected local StashLibrary folder below.
migrate_v163_cloud_mirror_to_single_folder()

def migrate_v169_webdav_backup_model():
    """Retire the desktop-cloud-folder sync mode without moving user files.

    Existing v0.10.164-168 users keep exactly the same StashLibrary folder path. The
    folder simply becomes Local Storage. The old .stashlibrary-cloud snapshot is left
    untouched as a recovery artefact, but StashLibrary stops updating it.
    """
    c=read_config(); changed=False
    if c.get('storage_sync_mode')!='local':
        c['storage_sync_mode']='local';changed=True
    if c.get('cloud_sync_enabled'):
        c['cloud_sync_enabled']=False;changed=True
    if c.get('cloud_sync_model') in {'single-folder-v1','provider-neutral-v1'}:
        c['cloud_sync_model']='retired-v0.10.169';changed=True
    if changed:write_config(c)

migrate_v169_webdav_backup_model()

def migrate_v165_storage_mode():
    c=read_config()
    mode=str(c.get('storage_sync_mode') or '').strip().lower()
    if mode not in {'local','synced'}:
        # v0.10.164 exposed this folder as Cloud Sync. Preserve that behaviour
        # on upgrade; users can now switch deliberately from Settings.
        mode='synced' if c.get('cloud_sync_enabled') else 'local'
        c['storage_sync_mode']=mode
        c['cloud_sync_enabled']=(mode=='synced')
        write_config(c)

migrate_v165_storage_mode()

# Fresh installs use StashLibrary at the root of the current user's drive.
ensure_default_library()

# Safely copy recognised historical default folders (StashLibrary, StashLibrary, or
# StashReadz) into the drive-root StashLibrary folder. Explicitly selected custom
# folders remain authoritative and are never moved automatically.
migrate_storage_root_to_userprofile()

# The StashLibrary folder is a complete local library.
migrate_internal_data_to_library()

def _load_history():
    hf=history_file()
    legacy=LEGACY_APPDIR/'history.json'
    try:
        source=hf if hf and hf.exists() else legacy
        d=json.loads(source.read_text(encoding='utf-8'))
        undo=[_portable_action(a) for a in list(d.get('undo') or [])]
        redo=[_portable_action(a) for a in list(d.get('redo') or [])]
        return undo,redo
    except:return [],[]

undo_stack,redo_stack=_load_history()
active_history_group_id=None



def _action_store(action):
    # A random ID prevents two rapid actions from ever reusing the same recovery folder,
    # even if history length changes because of undo/redo.
    ident=action.setdefault('id',uuid.uuid4().hex)
    base=undo_dir()
    if not base:raise RuntimeError('Choose a bookmarks folder first.')
    p=base/ident
    p.mkdir(parents=True,exist_ok=False)
    return p

def _drop_action_storage(action):
    try:
        if action.get('type')=='group':
            for child in list(action.get('actions') or []):
                _drop_action_storage(child)
            return
        base=undo_dir()
        p=(base/str(action.get('id',''))) if base else Path('__missing_undo_store__')
        if p.exists():shutil.rmtree(p,ignore_errors=True)
    except:pass

def _save_history():
    try:
        hf=history_file()
        if hf:hf.write_text(json.dumps({'undo':undo_stack[-HISTORY_LIMIT:],'redo':redo_stack[-HISTORY_LIMIT:]},indent=2,ensure_ascii=False),encoding='utf-8')
    except:pass

def _clear_redo():
    global redo_stack
    for a in redo_stack:_drop_action_storage(a)
    redo_stack=[];_save_history()

def begin_history_group(label='Batch operation'):
    global active_history_group_id,undo_stack
    # A popup reload/closed window or interrupted batch can leave the native
    # helper holding an active group even though no UI operation is running.
    # Finalise that stale group before beginning the next user operation.
    if active_history_group_id:
        end_history_group()
    _clear_redo()
    group={'type':'group','id':uuid.uuid4().hex,'label':str(label or 'Batch operation'),'actions':[]}
    active_history_group_id=group['id']
    undo_stack.append(group)
    while len(undo_stack)>HISTORY_LIMIT:
        old=undo_stack.pop(0);_drop_action_storage(old)
    _save_history()
    return history_state()

def end_history_group():
    global active_history_group_id,undo_stack
    gid=active_history_group_id
    active_history_group_id=None
    if gid:
        for i in range(len(undo_stack)-1,-1,-1):
            a=undo_stack[i]
            if a.get('type')=='group' and a.get('id')==gid:
                if not a.get('actions'):
                    undo_stack.pop(i)
                break
    _save_history()
    return history_state()

def record_action(action):
    global undo_stack
    action=_portable_action(action)
    if active_history_group_id:
        for a in reversed(undo_stack):
            if a.get('type')=='group' and a.get('id')==active_history_group_id:
                a.setdefault('actions',[]).append(action)
                _save_history()
                return
    _clear_redo()
    undo_stack.append(action)
    while len(undo_stack)>HISTORY_LIMIT:
        old=undo_stack.pop(0);_drop_action_storage(old)
    _save_history()

def _action_available(action,redo=False):
    """Whether the top history action still has the file/folder needed to run."""
    try:
        typ=action.get('type')
        if typ=='group':
            actions=list(action.get('actions') or [])
            return bool(actions) and all(_action_available(x,redo) for x in actions)
        if str(typ).startswith('flat_'):return True
        if not redo:
            if typ=='delete':return _resolve_history_path(action.get('stored')).exists()
            if typ=='created':return _resolve_history_path(action.get('path')).exists()
            if typ=='rename':
                return _resolve_history_path(action.get('new')).exists()
            if typ=='move':return _resolve_history_path(action.get('new')).exists()
            if typ=='reorder':return _resolve_history_path(action.get('parent')).exists()
        else:
            if typ=='delete':return _resolve_history_path(action.get('path')).exists()
            if typ=='created':return _resolve_history_path(action.get('stored')).exists()
            if typ=='rename':return _resolve_history_path(action.get('old')).exists()
            if typ=='move':return _resolve_history_path(action.get('old')).exists()
            if typ=='reorder':return _resolve_history_path(action.get('parent')).exists()
    except Exception:return False
    return False

def _prune_stale_history():
    """Drop impossible top entries instead of advertising an Undo/Redo that will fail."""
    changed=False
    while undo_stack and not _action_available(undo_stack[-1],False):
        stale=undo_stack.pop()
        _drop_action_storage(stale)
        changed=True
    while redo_stack and not _action_available(redo_stack[-1],True):
        stale=redo_stack.pop()
        _drop_action_storage(stale)
        changed=True
    if changed:_save_history()

def history_state():
    # Never mutate history merely because the popup asks whether Undo/Redo is
    # available. A transient path/state mismatch must not erase recoverable history.
    return {
        'canUndo':bool(undo_stack),'canRedo':bool(redo_stack),
        'undoLabel':undo_stack[-1].get('label','') if undo_stack else '',
        'redoLabel':redo_stack[-1].get('label','') if redo_stack else ''
    }

def _move_path(src,dest):
    src=Path(src);dest=Path(dest);dest.parent.mkdir(parents=True,exist_ok=True)
    if dest.exists():raise RuntimeError(f'Cannot restore because {dest.name} already exists.')
    shutil.move(str(src),str(dest));return dest

def _set_item_display(path,display):
    p=Path(path)
    if p.exists() and display:set_display_name(p.parent,p.name,display)
def _set_item_meta(path,meta=None,display=None):
    p=Path(path)
    if not p.exists():return
    entry=dict(meta or {})
    if display:entry['title']=display
    set_item_metadata(p.parent,p.name,title=entry.get('title'),source_url=entry.get('sourceUrl'),replace=True,extra={k:v for k,v in entry.items() if k not in {'title','sourceUrl'}})

def _restore_order(parent,names):
    p=Path(parent)
    if p.exists():save_order(p,list(names or []))

def _apply_history_action(a,redo=False):
    typ=a.get('type')
    if typ=='group':
        actions=list(a.get('actions') or [])
        ordered=actions if redo else list(reversed(actions))
        completed=[]
        try:
            for child in ordered:
                _apply_history_action(child,redo)
                completed.append(child)
        except Exception:
            # Roll back any part of the group already applied so an interrupted
            # group remains a single atomic history operation.
            for child in reversed(completed):
                try:_apply_history_action(child,not redo)
                except Exception:pass
            raise
        return
    if str(typ).startswith('flat_'):
        _flat_undo_action(a,redo);return
    if not redo:
        if typ=='delete':
            stored=_resolve_history_path(a['stored']);orig=_resolve_history_path(a['path']);_move_path(stored,orig);_set_item_meta(orig,a.get('meta'),a.get('display'));_restore_order(orig.parent,a.get('order_before'))
        elif typ=='created':
            cur=_resolve_history_path(a['path']);store=_action_store(a)/cur.name
            if cur.exists():
                remove_display_name(cur.parent,cur.name);_move_path(cur,store);a['stored']=_encode_history_path(store);_restore_order(cur.parent,a.get('order_before'))
        elif typ=='rename':
            new=_resolve_history_path(a['new']);old=_resolve_history_path(a['old'])
            if new.exists():
                if new.resolve()==old.resolve():
                    set_display_name(old.parent,old.name,a.get('display_old'));_restore_order(old.parent,a.get('order_before'))
                else:
                    remove_display_name(new.parent,new.name);_move_path(new,old);_set_item_meta(old,a.get('meta_old'),a.get('display_old'));_restore_order(old.parent,a.get('order_before'))
        elif typ=='move':
            new=_resolve_history_path(a['new']);old=_resolve_history_path(a['old'])
            if new.exists():
                _move_path(new,old)
                try:_db_move_node(new,old)
                except Exception:pass
                _set_item_meta(old,a.get('meta'),a.get('display'))
                _restore_order(_resolve_history_path(a['dest_parent']),a.get('dest_order_before'));_restore_order(old.parent,a.get('src_order_before'))
        elif typ=='reorder':_restore_order(_resolve_history_path(a['parent']),a.get('before'))
        else:raise RuntimeError('This action cannot be undone.')
    else:
        if typ=='delete':
            orig=_resolve_history_path(a['path']);store=_resolve_history_path(a['stored'])
            if orig.exists():
                remove_display_name(orig.parent,orig.name);_move_path(orig,store);_restore_order(orig.parent,a.get('order_after'))
        elif typ=='created':
            store=_resolve_history_path(a.get('stored',''));dest=_resolve_history_path(a['path'])
            if store.exists():_move_path(store,dest);_set_item_meta(dest,a.get('meta'),a.get('display'));_restore_order(dest.parent,a.get('order_after'))
        elif typ=='rename':
            old=_resolve_history_path(a['old']);new=_resolve_history_path(a['new'])
            if old.exists():
                if old.resolve()==new.resolve():
                    set_display_name(new.parent,new.name,a.get('display_new'));_restore_order(new.parent,a.get('order_after'))
                else:
                    remove_display_name(old.parent,old.name);_move_path(old,new);_set_item_meta(new,a.get('meta_new'),a.get('display_new'));_restore_order(new.parent,a.get('order_after'))
        elif typ=='move':
            old=_resolve_history_path(a['old']);new=_resolve_history_path(a['new'])
            if old.exists():
                _move_path(old,new)
                try:_db_move_node(old,new)
                except Exception:pass
                _set_item_meta(new,a.get('meta'),a.get('display'))
                _restore_order(old.parent,a.get('src_order_after'));_restore_order(new.parent,a.get('dest_order_after'))
        elif typ=='reorder':_restore_order(_resolve_history_path(a['parent']),a.get('after'))
        else:raise RuntimeError('This action cannot be redone.')

def undo_action():
    if not undo_stack:return {'changed':False,**history_state()}
    a=undo_stack.pop();label=a.get('label','Change')
    try:
        if not _action_available(a,False):
            raise RuntimeError(f'Undo data for “{label}” is unavailable. The history entry has been kept rather than discarded.')
        _apply_history_action(a,False)
        redo_stack.append(a);_save_history();return {'changed':True,'label':label,**history_state()}
    except Exception:
        undo_stack.append(a);_save_history();raise

def redo_action():
    if not redo_stack:return {'changed':False,**history_state()}
    a=redo_stack.pop();label=a.get('label','Change')
    try:
        if not _action_available(a,True):
            raise RuntimeError(f'Redo data for “{label}” is unavailable. The history entry has been kept rather than discarded.')
        _apply_history_action(a,True)
        undo_stack.append(a);_save_history();return {'changed':True,'label':label,**history_state()}
    except Exception:
        redo_stack.append(a);_save_history();raise

def migrate_folder_config():
    """Convert the v0.1.11 parent-root layout to two independent folder paths."""
    c=read_config()
    if c.get('bookmarks_path') or c.get('backups_path'):
        return
    old=c.get('root')
    if old:
        r=Path(old); b=r/'bookmarks'; z=r/'backups'
        b.mkdir(parents=True,exist_ok=True); z.mkdir(parents=True,exist_ok=True)
        c['bookmarks_path']=str(b); c['backups_path']=str(z); c['folder_config_version']=3
        write_config(c)
migrate_folder_config()

def backups_path():
    p=read_config().get('backups_path')
    return Path(p) if p else None

def _win_choose_folder(title, initialdir=''):
    """Show the modern native Windows folder picker (IFileOpenDialog).

    The old WinForms FolderBrowserDialog looked out of place and could also
    behave oddly around Firefox focus. IFileOpenDialog with FOS_PICKFOLDERS is
    the same Explorer-style folder chooser used by modern Windows apps. The
    Firefox window that is foreground when the user clicks the StashLibrary button is
    passed as the owner so the picker stays in front of StashLibrary.
    """
    if sys.platform!='win32':
        raise RuntimeError('The StashLibrary folder picker is currently available on Windows only.')

    import ctypes
    import uuid
    from ctypes import wintypes

    HRESULT=ctypes.c_long
    ULONG=ctypes.c_ulong
    DWORD=ctypes.c_ulong
    LPVOID=ctypes.c_void_p

    class GUID(ctypes.Structure):
        _fields_=[('Data1',ctypes.c_uint32),('Data2',ctypes.c_uint16),('Data3',ctypes.c_uint16),('Data4',ctypes.c_ubyte*8)]

    def guid(text):
        return GUID.from_buffer_copy(uuid.UUID(text).bytes_le)

    CLSID_FileOpenDialog=guid('DC1C5A9C-E88A-4DDE-A5A1-60F82A20AEF7')
    IID_IFileOpenDialog=guid('D57C7288-D4AD-4768-BE02-9D969532D960')
    IID_IShellItem=guid('43826D1E-E718-42EE-BC55-A1E261C37BFE')

    COINIT_APARTMENTTHREADED=0x2
    RPC_E_CHANGED_MODE=0x80010106
    CLSCTX_INPROC_SERVER=0x1
    FOS_PICKFOLDERS=0x00000020
    FOS_FORCEFILESYSTEM=0x00000040
    FOS_PATHMUSTEXIST=0x00000800
    FOS_NOCHANGEDIR=0x00000008
    SIGDN_FILESYSPATH=0x80058000
    ERROR_CANCELLED_HR=0x800704C7

    ole32=ctypes.OleDLL('ole32')
    shell32=ctypes.OleDLL('shell32')
    user32=ctypes.WinDLL('user32',use_last_error=True)

    ole32.CoInitializeEx.argtypes=[LPVOID,DWORD]
    ole32.CoInitializeEx.restype=HRESULT
    ole32.CoUninitialize.argtypes=[]
    ole32.CoUninitialize.restype=None
    ole32.CoCreateInstance.argtypes=[ctypes.POINTER(GUID),LPVOID,DWORD,ctypes.POINTER(GUID),ctypes.POINTER(LPVOID)]
    ole32.CoCreateInstance.restype=HRESULT
    ole32.CoTaskMemFree.argtypes=[LPVOID]
    ole32.CoTaskMemFree.restype=None
    shell32.SHCreateItemFromParsingName.argtypes=[wintypes.LPCWSTR,LPVOID,ctypes.POINTER(GUID),ctypes.POINTER(LPVOID)]
    shell32.SHCreateItemFromParsingName.restype=HRESULT
    user32.GetForegroundWindow.argtypes=[]
    user32.GetForegroundWindow.restype=wintypes.HWND

    def hr_u32(v):
        return ctypes.c_uint32(int(v)).value

    def failed(hr):
        return bool(hr_u32(hr)&0x80000000)

    def method(obj,index,restype,*argtypes):
        vtbl=ctypes.cast(obj,ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        return ctypes.WINFUNCTYPE(restype,ctypes.c_void_p,*argtypes)(vtbl[index])

    initialized=False
    dialog=LPVOID()
    initial_item=LPVOID()
    result_item=LPVOID()
    display_name=ctypes.c_wchar_p()
    try:
        hr=ole32.CoInitializeEx(None,COINIT_APARTMENTTHREADED)
        if hr_u32(hr)!=RPC_E_CHANGED_MODE:
            if failed(hr):raise RuntimeError(f'Windows COM initialization failed (0x{hr_u32(hr):08X}).')
            initialized=True

        hr=ole32.CoCreateInstance(ctypes.byref(CLSID_FileOpenDialog),None,CLSCTX_INPROC_SERVER,ctypes.byref(IID_IFileOpenDialog),ctypes.byref(dialog))
        if failed(hr) or not dialog.value:
            raise RuntimeError(f'Could not create the Windows folder picker (0x{hr_u32(hr):08X}).')

        # IFileDialog::GetOptions / SetOptions.
        options=DWORD()
        hr=method(dialog,10,HRESULT,ctypes.POINTER(DWORD))(dialog,ctypes.byref(options))
        if failed(hr):raise RuntimeError(f'Could not read folder-picker options (0x{hr_u32(hr):08X}).')
        opts=DWORD(options.value|FOS_PICKFOLDERS|FOS_FORCEFILESYSTEM|FOS_PATHMUSTEXIST|FOS_NOCHANGEDIR)
        hr=method(dialog,9,HRESULT,DWORD)(dialog,opts)
        if failed(hr):raise RuntimeError(f'Could not configure folder picker (0x{hr_u32(hr):08X}).')

        if title:
            hr=method(dialog,17,HRESULT,wintypes.LPCWSTR)(dialog,str(title))
            if failed(hr):raise RuntimeError(f'Could not set folder-picker title (0x{hr_u32(hr):08X}).')

        initial=str(initialdir or '').strip()
        if initial and Path(initial).is_dir():
            hr=shell32.SHCreateItemFromParsingName(initial,None,ctypes.byref(IID_IShellItem),ctypes.byref(initial_item))
            if not failed(hr) and initial_item.value:
                # IFileDialog::SetFolder makes the existing StashLibrary directory the
                # folder initially displayed, while still allowing navigation.
                method(dialog,12,HRESULT,LPVOID)(dialog,initial_item)

        owner=user32.GetForegroundWindow()
        hr=method(dialog,3,HRESULT,wintypes.HWND)(dialog,owner)
        if hr_u32(hr)==ERROR_CANCELLED_HR:return None
        if failed(hr):raise RuntimeError(f'Windows folder picker failed (0x{hr_u32(hr):08X}).')

        hr=method(dialog,20,HRESULT,ctypes.POINTER(LPVOID))(dialog,ctypes.byref(result_item))
        if failed(hr) or not result_item.value:
            raise RuntimeError(f'Windows folder picker returned no folder (0x{hr_u32(hr):08X}).')

        hr=method(result_item,5,HRESULT,ctypes.c_uint,ctypes.POINTER(ctypes.c_wchar_p))(result_item,SIGDN_FILESYSPATH,ctypes.byref(display_name))
        if failed(hr) or not display_name.value:
            raise RuntimeError(f'Could not read selected folder path (0x{hr_u32(hr):08X}).')
        return str(display_name.value)
    finally:
        if display_name:
            try:ole32.CoTaskMemFree(ctypes.cast(display_name,LPVOID))
            except Exception:pass
        for obj in (result_item,initial_item,dialog):
            if getattr(obj,'value',None):
                try:method(obj,2,ULONG)(obj)
                except Exception:pass
        if initialized:
            try:ole32.CoUninitialize()
            except Exception:pass


def _win_file_dialog(*,title,save=False,initialfile='',initialdir='',defaultext='zip'):
    """Show the modern native Windows file picker (IFileOpen/SaveDialog)."""
    if sys.platform!='win32':
        raise RuntimeError('The StashLibrary file picker is currently available on Windows only.')
    import ctypes
    import uuid
    from ctypes import wintypes

    HRESULT=ctypes.c_long;ULONG=ctypes.c_ulong;DWORD=ctypes.c_ulong;LPVOID=ctypes.c_void_p
    class GUID(ctypes.Structure):
        _fields_=[('Data1',ctypes.c_uint32),('Data2',ctypes.c_uint16),('Data3',ctypes.c_uint16),('Data4',ctypes.c_ubyte*8)]
    class COMDLG_FILTERSPEC(ctypes.Structure):
        _fields_=[('pszName',wintypes.LPCWSTR),('pszSpec',wintypes.LPCWSTR)]
    def guid(value):return GUID.from_buffer_copy(uuid.UUID(value).bytes_le)
    clsid=guid('C0B4E2F3-BA21-4773-8DBA-335EC946EB8B' if save else 'DC1C5A9C-E88A-4DDE-A5A1-60F82A20AEF7')
    iid=guid('84BCCd23-5FDE-4CDB-AEA4-AF64B83D78AB' if save else 'D57C7288-D4AD-4768-BE02-9D969532D960')
    iid_shell_item=guid('43826D1E-E718-42EE-BC55-A1E261C37BFE')
    ole32=ctypes.OleDLL('ole32');shell32=ctypes.OleDLL('shell32');user32=ctypes.WinDLL('user32',use_last_error=True)
    ole32.CoInitializeEx.argtypes=[LPVOID,DWORD];ole32.CoInitializeEx.restype=HRESULT
    ole32.CoUninitialize.argtypes=[];ole32.CoUninitialize.restype=None
    ole32.CoCreateInstance.argtypes=[ctypes.POINTER(GUID),LPVOID,DWORD,ctypes.POINTER(GUID),ctypes.POINTER(LPVOID)];ole32.CoCreateInstance.restype=HRESULT
    ole32.CoTaskMemFree.argtypes=[LPVOID];ole32.CoTaskMemFree.restype=None
    shell32.SHCreateItemFromParsingName.argtypes=[wintypes.LPCWSTR,LPVOID,ctypes.POINTER(GUID),ctypes.POINTER(LPVOID)];shell32.SHCreateItemFromParsingName.restype=HRESULT
    user32.GetForegroundWindow.argtypes=[];user32.GetForegroundWindow.restype=wintypes.HWND
    def hr_u32(value):return ctypes.c_uint32(int(value)).value
    def failed(hr):return bool(hr_u32(hr)&0x80000000)
    def method(obj,index,restype,*argtypes):
        vtbl=ctypes.cast(obj,ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        return ctypes.WINFUNCTYPE(restype,ctypes.c_void_p,*argtypes)(vtbl[index])

    initialized=False;dialog=LPVOID();initial_item=LPVOID();result_item=LPVOID();display_name=ctypes.c_wchar_p()
    filters=(COMDLG_FILTERSPEC*2)(COMDLG_FILTERSPEC('ZIP archive (*.zip)','*.zip'),COMDLG_FILTERSPEC('All files (*.*)','*.*'))
    try:
        hr=ole32.CoInitializeEx(None,0x2)
        if hr_u32(hr)!=0x80010106:
            if failed(hr):raise RuntimeError(f'Windows COM initialization failed (0x{hr_u32(hr):08X}).')
            initialized=True
        hr=ole32.CoCreateInstance(ctypes.byref(clsid),None,0x1,ctypes.byref(iid),ctypes.byref(dialog))
        if failed(hr) or not dialog.value:raise RuntimeError(f'Could not create the Windows file picker (0x{hr_u32(hr):08X}).')
        hr=method(dialog,4,HRESULT,ctypes.c_uint,ctypes.POINTER(COMDLG_FILTERSPEC))(dialog,2,filters)
        if failed(hr):raise RuntimeError(f'Could not configure file types (0x{hr_u32(hr):08X}).')
        options=DWORD();hr=method(dialog,10,HRESULT,ctypes.POINTER(DWORD))(dialog,ctypes.byref(options))
        if failed(hr):raise RuntimeError(f'Could not read file-picker options (0x{hr_u32(hr):08X}).')
        flags=0x40|0x800|0x8|(0x2 if save else 0x1000)
        hr=method(dialog,9,HRESULT,DWORD)(dialog,DWORD(options.value|flags))
        if failed(hr):raise RuntimeError(f'Could not configure file picker (0x{hr_u32(hr):08X}).')
        if title:
            hr=method(dialog,17,HRESULT,wintypes.LPCWSTR)(dialog,str(title))
            if failed(hr):raise RuntimeError(f'Could not set file-picker title (0x{hr_u32(hr):08X}).')
        if initialfile:
            hr=method(dialog,15,HRESULT,wintypes.LPCWSTR)(dialog,str(initialfile))
            if failed(hr):raise RuntimeError(f'Could not set the initial filename (0x{hr_u32(hr):08X}).')
        extension=str(defaultext or '').lstrip('.')
        if extension:
            hr=method(dialog,22,HRESULT,wintypes.LPCWSTR)(dialog,extension)
            if failed(hr):raise RuntimeError(f'Could not set the default extension (0x{hr_u32(hr):08X}).')
        initial=str(initialdir or '').strip()
        if initial and Path(initial).is_dir():
            hr=shell32.SHCreateItemFromParsingName(initial,None,ctypes.byref(iid_shell_item),ctypes.byref(initial_item))
            if not failed(hr) and initial_item.value:method(dialog,12,HRESULT,LPVOID)(dialog,initial_item)
        hr=method(dialog,3,HRESULT,wintypes.HWND)(dialog,user32.GetForegroundWindow())
        if hr_u32(hr)==0x800704C7:return None
        if failed(hr):raise RuntimeError(f'Windows file picker failed (0x{hr_u32(hr):08X}).')
        hr=method(dialog,20,HRESULT,ctypes.POINTER(LPVOID))(dialog,ctypes.byref(result_item))
        if failed(hr) or not result_item.value:raise RuntimeError(f'Windows file picker returned no file (0x{hr_u32(hr):08X}).')
        hr=method(result_item,5,HRESULT,ctypes.c_uint,ctypes.POINTER(ctypes.c_wchar_p))(result_item,0x80058000,ctypes.byref(display_name))
        if failed(hr) or not display_name.value:raise RuntimeError(f'Could not read selected file path (0x{hr_u32(hr):08X}).')
        return str(display_name.value)
    finally:
        if display_name:
            try:ole32.CoTaskMemFree(ctypes.cast(display_name,LPVOID))
            except Exception:pass
        for obj in (result_item,initial_item,dialog):
            if getattr(obj,'value',None):
                try:method(obj,2,ULONG)(obj)
                except Exception:pass
        if initialized:
            try:ole32.CoUninitialize()
            except Exception:pass
def choose_folder(kind):
    try:
        title='Choose StashLibrary storage folder' if kind=='bookmarks' else 'Choose StashLibrary backup folder'
        initial=read_config().get(f'{kind}_path') or ''
        p=_win_choose_folder(title,initial)
        if not p:return None
        path=Path(p);path.mkdir(parents=True,exist_ok=True)
        c=read_config();c[f'{kind}_path']=str(path);c['folder_config_version']=5 if uses_separated_internal_data() else 4
        if kind=='bookmarks':
            c['internal_data_layout']='library-local-v3';c['internal_data_path']=str(path/UNIFIED_DATA_DIR_NAME)
        write_config(c)
        if kind=='bookmarks':
            if uses_separated_internal_data():
                internal_data_dir(create=True)
            else:
                sysdir=path/SYSTEM_DIR_NAME;sysdir.mkdir(parents=True,exist_ok=True)
                legacy_store=LEGACY_APPDIR/'undo-store';new_store=sysdir/'undo-store'
                if legacy_store.exists() and not new_store.exists():
                    try:shutil.copytree(legacy_store,new_store)
                    except:pass
            # Normalize any old absolute history paths against the newly selected root.
            global undo_stack,redo_stack
            undo_stack=[_portable_action(a) for a in undo_stack]
            redo_stack=[_portable_action(a) for a in redo_stack]
            _save_history()
        return str(path)
    except Exception as e: raise RuntimeError(f'Folder picker failed: {e}')

def root_path():
    # Compatibility alias used by older UI/status code: the bookmark folder is the live tree root.
    return bookmarks_path()

def send(obj):
    raw=json.dumps(obj,ensure_ascii=False).encode('utf-8')
    with lock:
        sys.stdout.buffer.write(struct.pack('<I',len(raw)));sys.stdout.buffer.write(raw);sys.stdout.buffer.flush()

def progress(operation,stage='',detail='',percent=None,started=None):
    msg={'event':'progress','operation':operation,'stage':stage,'detail':detail}
    if percent is not None:msg['percent']=percent
    if started is not None:msg['elapsed']=time.monotonic()-started
    send(msg)

def capture_timestamp():
    # Store a timezone-aware ISO timestamp at the moment the archive is successfully
    # incorporated into StashLibrary. Zotero later receives this as Accessed.
    return datetime.now(timezone.utc).astimezone().isoformat(timespec='seconds')

class _ArchiveHTMLMetaParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.meta=[]; self.title_parts=[]; self.in_title=False; self.jsonld=[]; self.in_jsonld=False; self._json_parts=[]
    def handle_starttag(self, tag, attrs):
        tag=tag.lower(); a={str(k).lower():str(v or '') for k,v in attrs}
        if tag=='meta':
            key=(a.get('name') or a.get('property') or a.get('itemprop') or '').strip().lower()
            val=(a.get('content') or '').strip()
            if key and val:self.meta.append((key,val))
        elif tag=='title': self.in_title=True
        elif tag=='script' and 'ld+json' in a.get('type','').lower():
            self.in_jsonld=True;self._json_parts=[]
    def handle_endtag(self, tag):
        tag=tag.lower()
        if tag=='title':self.in_title=False
        elif tag=='script' and self.in_jsonld:
            self.in_jsonld=False
            raw=''.join(self._json_parts).strip()
            if raw:self.jsonld.append(raw)
            self._json_parts=[]
    def handle_data(self, data):
        if self.in_title:self.title_parts.append(data)
        if self.in_jsonld:self._json_parts.append(data)

def _clean_text(v):
    return re.sub(r'\s+',' ',html.unescape(str(v or ''))).strip()

def _jsonld_walk(obj, titles, authors):
    if isinstance(obj,list):
        for x in obj:_jsonld_walk(x,titles,authors)
        return
    if not isinstance(obj,dict):return
    for k in ('headline','name'):
        v=obj.get(k)
        if isinstance(v,str) and v.strip():titles.append(_clean_text(v))
    av=obj.get('author')
    if av is not None:
        vals=av if isinstance(av,list) else [av]
        for a in vals:
            if isinstance(a,str):authors.append(_clean_text(a))
            elif isinstance(a,dict):
                nm=a.get('name')
                if isinstance(nm,str) and nm.strip():authors.append(_clean_text(nm))
                else:
                    given=_clean_text(a.get('givenName')); family=_clean_text(a.get('familyName'))
                    if given or family:authors.append((given+' '+family).strip())
    graph=obj.get('@graph')
    if graph is not None:_jsonld_walk(graph,titles,authors)

def extract_archive_metadata(path):
    p=Path(path)
    if p.suffix.lower() not in {'.html','.htm'} or not p.is_file():return {'title':'','authors':[]}
    try:
        # SingleFile archives can be large. Metadata normally lives in the head, but
        # read the whole file so JSON-LD/meta inserted later is still recoverable.
        raw=p.read_bytes()
        text=raw.decode('utf-8',errors='replace')
    except Exception:return {'title':'','authors':[]}
    parser=_ArchiveHTMLMetaParser()
    try:parser.feed(text)
    except Exception:pass
    metas={}
    for k,v in parser.meta:metas.setdefault(k,[]).append(_clean_text(v))
    title_candidates=[]
    for key in ('citation_title','dc.title','dcterms.title','og:title','twitter:title'):
        title_candidates += [x for x in metas.get(key,[]) if x]
    authors=[]
    for key in ('citation_author','author','dc.creator','dcterms.creator','parsely-author','byl'):
        authors += [x for x in metas.get(key,[]) if x]
    json_titles=[];json_authors=[]
    for block in parser.jsonld:
        try:_jsonld_walk(json.loads(block),json_titles,json_authors)
        except Exception:pass
    title_candidates += json_titles
    if not title_candidates:
        t=_clean_text(''.join(parser.title_parts))
        if t:title_candidates.append(t)
    authors += json_authors
    # Deduplicate while preserving page order.
    seen=set();clean_auth=[]
    for a in authors:
        a=_clean_text(a)
        key=a.casefold()
        if a and key not in seen:
            seen.add(key);clean_auth.append(a)
    return {'title': next((x for x in title_candidates if x),''), 'authors':clean_auth[:50]}

def safe_name(s, fallback='Bookmark'):
    s=re.sub(r'[<>:"/\\|?*\x00-\x1f]','_',s or '').strip().rstrip('.')
    return (s[:120] or fallback)

def metadata_file(folder): return Path(folder)/'.local-bookmarks-meta.json'
def order_file(folder): return Path(folder)/'.local-bookmarks-order.json'

def _normalise_meta_entry(value):
    if isinstance(value,dict):
        out={}
        title=str(value.get('title') or '').strip()
        source=str(value.get('sourceUrl') or value.get('sourceURL') or '').strip()
        accessed=str(value.get('accessedAt') or value.get('accessed') or '').strip()
        if title:out['title']=title
        if source:out['sourceUrl']=source
        if accessed:out['accessedAt']=accessed
        for k,v in value.items():
            if k not in {'title','sourceUrl','sourceURL','accessedAt','accessed'} and v not in (None,''):
                out[k]=v
        return out
    title=str(value or '').strip()
    return {'title':title} if title else {}

def _raw_load_metadata(folder):
    try:
        raw=json.loads(metadata_file(folder).read_text(encoding='utf-8'))
        if not isinstance(raw,dict):return {}
        return {str(k):_normalise_meta_entry(v) for k,v in raw.items()}
    except:return {}

def _raw_write_metadata(folder,data):
    folder=Path(folder)
    if not folder.exists():return
    try:
        clean={}
        for k,v in (data or {}).items():
            entry=_normalise_meta_entry(v)
            if entry:clean[str(k)]=entry
        if clean:
            metadata_file(folder).write_text(json.dumps(clean,indent=2,ensure_ascii=False),encoding='utf-8')
        elif metadata_file(folder).exists():
            metadata_file(folder).unlink()
    except:pass

def _raw_load_order(folder):
    try:
        raw=json.loads(order_file(folder).read_text(encoding='utf-8'))
        return [str(x) for x in raw] if isinstance(raw,list) else []
    except:return []

def _raw_write_order(folder,names):
    folder=Path(folder)
    if not folder.exists():return
    try:order_file(folder).write_text(json.dumps(list(names or []),indent=2),encoding='utf-8')
    except:pass

def _raw_visible_entries(folder):
    folder=Path(folder)
    if not folder.exists():return []
    return [p for p in folder.iterdir() if p.name not in {SYSTEM_DIR_NAME,UNIFIED_DATA_DIR_NAME,LEGACY_UNIFIED_DATA_DIR_NAME,CLOUD_META_DIR} and not p.name.startswith('.local-bookmarks-')]

def database_file(root=None):
    # v0.10.171 keeps the live SQLite catalogue with the rest of the
    # library data inside <StashLibrary folder>/.stashlibrary-data.
    p=internal_data_dir(root=root,create=True)
    return (p/DATABASE_NAME) if p else None

def _db_table_columns(conn,table):
    try:
        return {str(r['name']) for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    except Exception:
        return set()

def _stable_guid(prefix=''):
    # Opaque stable IDs, similar in purpose to Firefox bookmark GUIDs/Zotero keys.
    return f"{prefix}{uuid.uuid4().hex}"

def _db_schema(conn):
    """Create/migrate the StashLibrary catalogue.

    Schema v2 separates logical bookmark nodes from physical files:
      nodes  = hierarchy, title, ordering, URL, accessed date
      files  = physical archive filename and file identity

    physical_name remains as a compatibility mirror for old code/recovery, but
    file_id -> files.storage_name is the new attachment identity.
    """
    conn.execute('PRAGMA foreign_keys=ON')
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA synchronous=NORMAL')

    # Keep the original columns so an existing v1 database can be migrated
    # in-place without recreating the catalogue.
    conn.execute(
        "CREATE TABLE IF NOT EXISTS nodes("
        "id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "parent_id INTEGER REFERENCES nodes(id) ON DELETE CASCADE,"
        "physical_name TEXT NOT NULL,"
        "kind TEXT NOT NULL,"
        "display_name TEXT NOT NULL,"
        "source_url TEXT NOT NULL DEFAULT '',"
        "accessed_at TEXT NOT NULL DEFAULT '',"
        "position INTEGER NOT NULL DEFAULT 0,"
        "extra_json TEXT NOT NULL DEFAULT '{}',"
        "created_at TEXT NOT NULL DEFAULT '',"
        "UNIQUE(parent_id,physical_name))"
    )

    conn.execute(
        "CREATE TABLE IF NOT EXISTS files("
        "id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "file_guid TEXT NOT NULL UNIQUE,"
        "storage_name TEXT NOT NULL UNIQUE,"
        "extension TEXT NOT NULL DEFAULT '',"
        "size_bytes INTEGER NOT NULL DEFAULT 0,"
        "sha256 TEXT NOT NULL DEFAULT '',"
        "created_at TEXT NOT NULL DEFAULT '',"
        "modified_at TEXT NOT NULL DEFAULT '')"
    )

    # SQLite ALTER TABLE is deliberately used for migration so the existing
    # node IDs and hierarchy survive unchanged.
    file_cols=_db_table_columns(conn,'files')
    if 'fs_dev' not in file_cols:
        conn.execute("ALTER TABLE files ADD COLUMN fs_dev TEXT NOT NULL DEFAULT ''")
    if 'fs_ino' not in file_cols:
        conn.execute("ALTER TABLE files ADD COLUMN fs_ino TEXT NOT NULL DEFAULT ''")

    cols=_db_table_columns(conn,'nodes')
    if 'node_guid' not in cols:
        conn.execute("ALTER TABLE nodes ADD COLUMN node_guid TEXT")
    if 'file_id' not in cols:
        conn.execute("ALTER TABLE nodes ADD COLUMN file_id INTEGER REFERENCES files(id) ON DELETE SET NULL")
    if 'modified_at' not in cols:
        conn.execute("ALTER TABLE nodes ADD COLUMN modified_at TEXT NOT NULL DEFAULT ''")

    conn.execute(
        "CREATE TABLE IF NOT EXISTS catalogue_meta("
        "key TEXT PRIMARY KEY,"
        "value TEXT NOT NULL)"
    )

    root=conn.execute("SELECT id FROM nodes WHERE kind='root' LIMIT 1").fetchone()
    if not root:
        now=capture_timestamp()
        conn.execute(
            "INSERT INTO nodes(id,parent_id,physical_name,kind,display_name,position,extra_json,created_at,node_guid,modified_at) "
            "VALUES(1,NULL,'','root','StashLibrary root',0,'{}',?,?,?)",
            (now,_stable_guid('n_'),now)
        )

    now=capture_timestamp()

    # Stable logical IDs for every existing node.
    for row in conn.execute("SELECT id FROM nodes WHERE node_guid IS NULL OR node_guid=''").fetchall():
        conn.execute(
            "UPDATE nodes SET node_guid=?,modified_at=CASE WHEN modified_at='' THEN ? ELSE modified_at END WHERE id=?",
            (_stable_guid('n_'),now,int(row['id']))
        )

    # Migrate every existing bookmark into the independent files table.
    bookmarks=conn.execute(
        "SELECT id,physical_name,file_id,created_at FROM nodes WHERE kind='bookmark'"
    ).fetchall()
    arc=flat_archive_dir()
    for row in bookmarks:
        storage=str(row['physical_name'] or '')
        if not storage:
            continue

        file_row=None
        if row['file_id'] is not None:
            file_row=conn.execute("SELECT id FROM files WHERE id=?",(int(row['file_id']),)).fetchone()
        if not file_row:
            file_row=conn.execute("SELECT id FROM files WHERE storage_name=?",(storage,)).fetchone()

        if file_row:
            fid=int(file_row['id'])
        else:
            p=(arc/storage) if arc else None
            size=0
            try:
                if p and p.is_file():size=int(p.stat().st_size)
            except Exception:
                size=0
            ext=Path(storage).suffix.lower()
            cur=conn.execute(
                "INSERT INTO files(file_guid,storage_name,extension,size_bytes,created_at,modified_at) "
                "VALUES(?,?,?,?,?,?)",
                (_stable_guid('f_'),storage,ext,size,str(row['created_at'] or now),now)
            )
            fid=int(cur.lastrowid)

        conn.execute(
            "UPDATE nodes SET file_id=?,modified_at=CASE WHEN modified_at='' THEN ? ELSE modified_at END WHERE id=?",
            (fid,now,int(row['id']))
        )

    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_nodes_guid ON nodes(node_guid)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_nodes_parent_position ON nodes(parent_id,position,id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_nodes_file_id ON nodes(file_id)")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_files_guid ON files(file_guid)")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_files_storage_name ON files(storage_name)")

    conn.execute(
        "INSERT OR REPLACE INTO catalogue_meta(key,value) VALUES('schema_version',?)",
        (str(DATABASE_SCHEMA_VERSION),)
    )
    conn.execute(
        "INSERT OR REPLACE INTO catalogue_meta(key,value) VALUES('storage_model',?)",
        ('places-like-nodes+zotero-like-files',)
    )
    library_row=conn.execute("SELECT value FROM catalogue_meta WHERE key='library_id' LIMIT 1").fetchone()
    if not library_row or not str(library_row['value'] or '').strip():
        conn.execute("INSERT OR REPLACE INTO catalogue_meta(key,value) VALUES('library_id',?)",(_stable_guid('lib_'),))


def _promote_flat_layout_config():
    c=read_config()
    if c.get('storage_layout')=='flat-sqlite-v1':
        c['storage_layout']='flat-sqlite-v2'
        c['storage_model']='places-like-nodes+zotero-like-files'
        c['storage_schema_migrated_at']=capture_timestamp()
        write_config(c)

def _db_connect():
    db=database_file()
    if not db:raise RuntimeError('Choose a bookmarks folder first.')
    try:
        conn=sqlite3.connect(str(db),timeout=20)
        conn.row_factory=sqlite3.Row
        _db_schema(conn)
        _promote_flat_layout_config()
        return conn
    except sqlite3.DatabaseError:
        try:
            if db.exists():
                broken=db.with_name(f'stashlibrary-corrupt-{datetime.now().strftime("%Y%m%d-%H%M%S")}.sqlite3')
                db.replace(broken)
            for suffix in ('-wal','-shm'):
                side=Path(str(db)+suffix)
                if side.exists():side.unlink()
        except Exception:pass
        conn=sqlite3.connect(str(db),timeout=20)
        conn.row_factory=sqlite3.Row
        _db_schema(conn)
        _promote_flat_layout_config()
        return conn

def _meta_to_columns(entry,fallback=''):
    entry=_normalise_meta_entry(entry)
    title=str(entry.get('title') or fallback or '').strip()
    source=str(entry.get('sourceUrl') or '').strip()
    accessed=str(entry.get('accessedAt') or '').strip()
    extra={k:v for k,v in entry.items() if k not in {'title','sourceUrl','accessedAt'}}
    return title,source,accessed,json.dumps(extra,ensure_ascii=False)

def _row_to_meta(row):
    out={}
    if row['display_name']:out['title']=row['display_name']
    if row['source_url']:out['sourceUrl']=row['source_url']
    if row['accessed_at']:out['accessedAt']=row['accessed_at']
    try:
        extra=json.loads(row['extra_json'] or '{}')
        if isinstance(extra,dict):out.update(extra)
    except:pass
    return out

def _db_root_id(conn):
    row=conn.execute("SELECT id FROM nodes WHERE kind='root' LIMIT 1").fetchone()
    return int(row['id'])

def _db_folder_id(conn,folder,create_missing=False):
    root=bookmarks_path()
    if not root:return None
    root=root.resolve()
    folder=Path(folder).resolve(strict=False)
    if folder==root:return _db_root_id(conn)
    try:rel=folder.relative_to(root)
    except ValueError:return None
    parent=_db_root_id(conn)
    current=root
    for part in rel.parts:
        current=current/part
        row=conn.execute(
            "SELECT id FROM nodes WHERE parent_id=? AND physical_name=? AND kind='folder'",
            (parent,part)
        ).fetchone()
        if not row and create_missing and current.exists() and current.is_dir():
            meta=_raw_load_metadata(current.parent).get(part) or {}
            title,source,accessed,extra=_meta_to_columns(meta,_strip_folder_timestamp(part))
            pos=conn.execute("SELECT COALESCE(MAX(position),-1)+1 n FROM nodes WHERE parent_id=?",(parent,)).fetchone()['n']
            cur=conn.execute(
                "INSERT INTO nodes(parent_id,physical_name,kind,display_name,source_url,accessed_at,position,extra_json,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (parent,part,'folder',title,source,accessed,int(pos),extra,capture_timestamp())
            )
            row={'id':cur.lastrowid}
        if not row:return None
        parent=int(row['id'])
    return parent

def _db_node_for_path(conn,path):
    p=Path(path)
    root=bookmarks_path()
    if not root:return None
    if p.resolve(strict=False)==root.resolve(strict=False):
        return conn.execute("SELECT * FROM nodes WHERE kind='root' LIMIT 1").fetchone()
    parent_id=_db_folder_id(conn,p.parent,create_missing=True)
    if parent_id is None:return None
    return conn.execute(
        "SELECT * FROM nodes WHERE parent_id=? AND physical_name=?",
        (parent_id,p.name)
    ).fetchone()

def _db_import_folder(conn,folder,parent_id):
    folder=Path(folder)
    meta=_raw_load_metadata(folder)
    order=_raw_load_order(folder)
    entries=_raw_visible_entries(folder)
    ranks={n:i for i,n in enumerate(order)}
    entries.sort(key=lambda p:(ranks.get(p.name,10**9),p.name.lower()))
    for pos,p in enumerate(entries):
        entry=meta.get(p.name) or {}
        fallback=_strip_folder_timestamp(p.name) if p.is_dir() else _strip_existing_archive_stamp(p.stem)
        title,source,accessed,extra=_meta_to_columns(entry,fallback)
        try:created=datetime.fromtimestamp(p.stat().st_ctime).astimezone().isoformat()
        except:created=capture_timestamp()
        conn.execute(
            "INSERT OR IGNORE INTO nodes(parent_id,physical_name,kind,display_name,source_url,accessed_at,position,extra_json,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (parent_id,p.name,'folder' if p.is_dir() else 'bookmark',title,source,accessed,pos,extra,created)
        )
        row=conn.execute(
            "SELECT id FROM nodes WHERE parent_id=? AND physical_name=?",
            (parent_id,p.name)
        ).fetchone()
        if p.is_dir() and row:
            _db_import_folder(conn,p,int(row['id']))

def _db_ensure_imported(conn):
    marker=conn.execute("SELECT value FROM catalogue_meta WHERE key='initial_import_complete'").fetchone()
    if marker:return
    root=bookmarks_path()
    if root and root.exists():
        _db_import_folder(conn,root,_db_root_id(conn))
    conn.execute(
        "INSERT OR REPLACE INTO catalogue_meta(key,value) VALUES('initial_import_complete',?)",
        (capture_timestamp(),)
    )

def _db_sync_folder(conn,folder):
    if is_flat_layout():return
    folder=Path(folder)
    if not folder.exists():return
    parent_id=_db_folder_id(conn,folder,create_missing=True)
    if parent_id is None:return
    actual={p.name:p for p in _raw_visible_entries(folder)}
    rows=conn.execute("SELECT * FROM nodes WHERE parent_id=? ORDER BY position,id",(parent_id,)).fetchall()
    known={r['physical_name']:r for r in rows}
    for name,row in known.items():
        if name not in actual:
            conn.execute("DELETE FROM nodes WHERE id=?",(row['id'],))
    maxpos=conn.execute("SELECT COALESCE(MAX(position),-1) n FROM nodes WHERE parent_id=?",(parent_id,)).fetchone()['n']
    meta=_raw_load_metadata(folder)
    for name,p in actual.items():
        if name in known:continue
        maxpos+=1
        entry=meta.get(name) or {}
        fallback=_strip_folder_timestamp(name) if p.is_dir() else _strip_existing_archive_stamp(p.stem)
        title,source,accessed,extra=_meta_to_columns(entry,fallback)
        conn.execute(
            "INSERT INTO nodes(parent_id,physical_name,kind,display_name,source_url,accessed_at,position,extra_json,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (parent_id,name,'folder' if p.is_dir() else 'bookmark',title,source,accessed,int(maxpos),extra,capture_timestamp())
        )

def _db_write_folder_mirrors(conn,folder):
    folder=Path(folder)
    if not folder.exists():return
    parent_id=_db_folder_id(conn,folder,create_missing=False)
    if parent_id is None:return
    rows=conn.execute("SELECT * FROM nodes WHERE parent_id=? ORDER BY position,id",(parent_id,)).fetchall()
    _raw_write_metadata(folder,{r['physical_name']:_row_to_meta(r) for r in rows})
    _raw_write_order(folder,[r['physical_name'] for r in rows])

def _with_db(fn):
    with DB_LOCK:
        conn=_db_connect()
        try:
            _db_ensure_imported(conn)
            result=fn(conn)
            conn.commit()
            return result
        finally:
            conn.close()

def load_metadata(folder):
    folder=Path(folder)
    def op(conn):
        _db_sync_folder(conn,folder)
        parent_id=_db_folder_id(conn,folder,create_missing=True)
        if parent_id is None:return {}
        rows=conn.execute("SELECT * FROM nodes WHERE parent_id=?",(parent_id,)).fetchall()
        return {r['physical_name']:_row_to_meta(r) for r in rows}
    try:return _with_db(op)
    except:return _raw_load_metadata(folder)

def save_metadata(folder,data):
    folder=Path(folder)
    def op(conn):
        _db_sync_folder(conn,folder)
        parent_id=_db_folder_id(conn,folder,create_missing=True)
        if parent_id is None:return
        actual={p.name:p for p in _raw_visible_entries(folder)}
        for key,value in (data or {}).items():
            key=str(key)
            if key not in actual:continue
            row=conn.execute("SELECT * FROM nodes WHERE parent_id=? AND physical_name=?",(parent_id,key)).fetchone()
            if not row:continue
            fallback=_strip_folder_timestamp(key) if actual[key].is_dir() else _strip_existing_archive_stamp(actual[key].stem)
            title,source,accessed,extra=_meta_to_columns(value,fallback)
            conn.execute(
                "UPDATE nodes SET display_name=?,source_url=?,accessed_at=?,extra_json=? WHERE id=?",
                (title,source,accessed,extra,row['id'])
            )
        _db_write_folder_mirrors(conn,folder)
    try:_with_db(op)
    except Exception:_raw_write_metadata(folder,data)

def get_item_metadata(path):
    p=Path(path)
    def op(conn):
        _db_sync_folder(conn,p.parent)
        row=_db_node_for_path(conn,p)
        return _row_to_meta(row) if row else {}
    try:return dict(_with_db(op) or {})
    except:return dict(_raw_load_metadata(p.parent).get(p.name) or {})

def set_item_metadata(folder,physical_name,title=None,source_url=None,accessed_at=None,replace=False,extra=None):
    folder=Path(folder);key=str(physical_name)
    def op(conn):
        _db_sync_folder(conn,folder)
        parent_id=_db_folder_id(conn,folder,create_missing=True)
        if parent_id is None:return
        p=folder/key
        row=conn.execute("SELECT * FROM nodes WHERE parent_id=? AND physical_name=?",(parent_id,key)).fetchone()
        if not row and p.exists():
            current_max=conn.execute("SELECT COALESCE(MAX(position),-1)+1 n FROM nodes WHERE parent_id=?",(parent_id,)).fetchone()['n']
            fallback=_strip_folder_timestamp(key) if p.is_dir() else _strip_existing_archive_stamp(p.stem)
            cur=conn.execute(
                "INSERT INTO nodes(parent_id,physical_name,kind,display_name,position,created_at) VALUES(?,?,?,?,?,?)",
                (parent_id,key,'folder' if p.is_dir() else 'bookmark',fallback,int(current_max),capture_timestamp())
            )
            row=conn.execute("SELECT * FROM nodes WHERE id=?",(cur.lastrowid,)).fetchone()
        if not row:return
        entry={} if replace else _row_to_meta(row)
        if title is not None:
            t=str(title or '').strip()
            if t:entry['title']=t
            else:entry.pop('title',None)
        if source_url is not None:
            s=str(source_url or '').strip()
            if s:entry['sourceUrl']=s
            else:entry.pop('sourceUrl',None)
        if accessed_at is not None:
            a=str(accessed_at or '').strip()
            if a:entry['accessedAt']=a
            else:entry.pop('accessedAt',None)
        if isinstance(extra,dict):
            for k,v in extra.items():
                if v in (None,''):entry.pop(k,None)
                else:entry[k]=v
        fallback=_strip_folder_timestamp(key) if p.is_dir() else _strip_existing_archive_stamp(p.stem)
        t,s,a,x=_meta_to_columns(entry,fallback)
        conn.execute("UPDATE nodes SET display_name=?,source_url=?,accessed_at=?,extra_json=? WHERE id=?",(t,s,a,x,row['id']))
        _db_write_folder_mirrors(conn,folder)
    _with_db(op)

def set_display_name(folder,physical_name,display_name):
    display=str(display_name or '').strip()
    if display:set_item_metadata(folder,physical_name,title=display)

def set_source_url(folder,physical_name,source_url):
    set_item_metadata(folder,physical_name,source_url=source_url)

def remove_display_name(folder,physical_name):
    folder=Path(folder);key=str(physical_name)
    def op(conn):
        parent_id=_db_folder_id(conn,folder,create_missing=False)
        if parent_id is not None:
            conn.execute("DELETE FROM nodes WHERE parent_id=? AND physical_name=?",(parent_id,key))
            _db_write_folder_mirrors(conn,folder)
    try:_with_db(op)
    except Exception:
        data=_raw_load_metadata(folder);data.pop(key,None);_raw_write_metadata(folder,data)

def get_display_name(path):
    p=Path(path)
    entry=get_item_metadata(p)
    fallback=p.name if p.is_dir() else p.stem
    return str(entry.get('title') or fallback)

def get_source_url(path):
    return str(get_item_metadata(path).get('sourceUrl') or '')

def normalized_display_title(title,suffix=''):
    t=str(title or '').strip()
    t=re.sub(r'\s*[—-]\s*Mozilla Firefox\s*$','',t,flags=re.I).strip()
    if suffix and t.lower().endswith(str(suffix).lower()):
        t=t[:-len(str(suffix))].rstrip()
    return t

def load_order(folder):
    folder=Path(folder)
    def op(conn):
        _db_sync_folder(conn,folder)
        parent_id=_db_folder_id(conn,folder,create_missing=True)
        if parent_id is None:return []
        rows=conn.execute("SELECT physical_name FROM nodes WHERE parent_id=? ORDER BY position,id",(parent_id,)).fetchall()
        return [r['physical_name'] for r in rows]
    try:return _with_db(op)
    except:return _raw_load_order(folder)

def save_order(folder,names):
    folder=Path(folder)
    def op(conn):
        _db_sync_folder(conn,folder)
        parent_id=_db_folder_id(conn,folder,create_missing=True)
        if parent_id is None:return
        rows=conn.execute("SELECT physical_name FROM nodes WHERE parent_id=? ORDER BY position,id",(parent_id,)).fetchall()
        existing=[r['physical_name'] for r in rows]
        clean=[]
        for n in names or []:
            n=str(n)
            if n in existing and n not in clean:clean.append(n)
        for n in existing:
            if n not in clean:clean.append(n)
        for pos,n in enumerate(clean):
            conn.execute("UPDATE nodes SET position=? WHERE parent_id=? AND physical_name=?",(pos,parent_id,n))
        _db_write_folder_mirrors(conn,folder)
    try:_with_db(op)
    except Exception:_raw_write_order(folder,names)

def visible_entries(folder):
    folder=Path(folder)
    if not folder.exists():return []
    vals=_raw_visible_entries(folder)
    order=load_order(folder)
    ranks={n:i for i,n in enumerate(order)}
    vals.sort(key=lambda p:(ranks.get(p.name,10**9),p.name.lower()))
    return vals

def _db_move_node(old_path,new_path):
    old=Path(old_path);new=Path(new_path)
    def op(conn):
        row=_db_node_for_path(conn,old)
        if not row:return False
        new_parent=_db_folder_id(conn,new.parent,create_missing=True)
        if new_parent is None:return False
        pos=conn.execute("SELECT COALESCE(MAX(position),-1)+1 n FROM nodes WHERE parent_id=?",(new_parent,)).fetchone()['n']
        conn.execute("UPDATE nodes SET parent_id=?,physical_name=?,position=? WHERE id=?",(new_parent,new.name,int(pos),row['id']))
        _db_write_folder_mirrors(conn,old.parent)
        _db_write_folder_mirrors(conn,new.parent)
        return True
    return bool(_with_db(op))

def _flat_recovery_node_count():
    f=flat_recovery_file()
    if not f or not f.exists():return 0
    try:
        data=json.loads(f.read_text(encoding='utf-8'))
        rows=data.get('nodes') or []
        return len([r for r in rows if str(r.get('kind'))!='root'])
    except:return 0

def _flat_catalogue_status(conn):
    db_count=int(conn.execute("SELECT COUNT(*) n FROM nodes WHERE kind!='root'").fetchone()['n'])
    recovery_count=_flat_recovery_node_count() if is_flat_layout() else 0
    mismatch=bool(is_flat_layout() and recovery_count>db_count)
    return db_count,recovery_count,mismatch


def repair_catalogue_file_links():
    """Repair all current bookmark -> files-table relationships."""
    if not is_flat_layout():
        return {'checked':0,'repaired':0}

    def op(conn):
        rows=[
            dict(r) for r in conn.execute(
                "SELECT id,kind,physical_name,file_id,created_at FROM nodes "
                "WHERE kind='bookmark' ORDER BY id"
            ).fetchall()
        ]
        repaired=_repair_restored_bookmark_file_links(conn,rows)
        _flat_write_recovery(conn)
        return {'checked':len(rows),'repaired':repaired}

    return _with_db(op)


def repair_scan():
    """Compare bookmark records, file records and physical archive files.

    User-facing classification:
      - recordsWithoutFiles:
          a bookmark/file-record exists in SQLite, but its physical file is missing
          OR a bookmark lacks a valid file relationship.
      - filesWithoutRecords:
          a physical file exists in the archive but no bookmark/file record owns it.

    Orphan file-table rows whose physical file is missing are also records without
    files, because there is no physical file to act on.
    """
    if not is_flat_layout():
        return {
            'archiveFiles':0,'bookmarkRows':0,'fileRows':0,
            'recordsWithoutFiles':[],'filesWithoutRecords':[],
            'metadataIssues':[],'issues':[],'summary':{}
        }

    def op(conn):
        arc=flat_archive_dir()

        physical={}
        if arc and arc.exists():
            for p in arc.iterdir():
                if p.is_file() and not p.name.startswith('.local-bookmarks-'):
                    physical[p.name]={
                        'name':p.name,
                        'size':int(p.stat().st_size),
                        'ext':p.suffix.lower()
                    }

        file_rows=[dict(r) for r in conn.execute(
            "SELECT id,file_guid,storage_name,extension,size_bytes,sha256,fs_dev,fs_ino,created_at,modified_at "
            "FROM files ORDER BY id"
        ).fetchall()]

        node_rows=[dict(r) for r in conn.execute(
            "SELECT id,node_guid,parent_id,display_name,physical_name,file_id,source_url,accessed_at,created_at "
            "FROM nodes WHERE kind='bookmark' ORDER BY id"
        ).fetchall()]

        parent_rows={int(r['id']):dict(r) for r in conn.execute("SELECT id,parent_id,kind FROM nodes").fetchall()}
        root_id=_db_root_id(conn)
        def node_virtual_ref(node_id):
            ids=[];cur=int(node_id);seen=set()
            while cur and cur!=root_id and cur not in seen:
                seen.add(cur);ids.append(cur)
                row=parent_rows.get(cur);cur=int(row.get('parent_id') or 0) if row else 0
            ids.reverse();return _virtual_ref_from_ids(ids) if ids else VIRTUAL_ROOT
        for nr in node_rows:nr['virtual_path']=node_virtual_ref(nr['id'])

        files_by_id={int(r['id']):r for r in file_rows}
        files_by_name={str(r['storage_name'] or ''):r for r in file_rows}
        nodes_by_file_id={}
        for nr in node_rows:
            if nr.get('file_id') is not None:
                nodes_by_file_id.setdefault(int(nr['file_id']),[]).append(nr)

        records_without_files=[]
        files_without_records=[]
        metadata_issues=[]

        # 1) Bookmark records that cannot resolve to an existing physical file.
        for nr in node_rows:
            fid=nr.get('file_id')
            title=str(nr.get('display_name') or '')
            legacy=str(nr.get('physical_name') or '')
            fr=files_by_id.get(int(fid)) if fid is not None else None
            storage=str(fr.get('storage_name') or '') if fr else legacy

            if fid is None:
                records_without_files.append({
                    'kind':'recordWithoutFile',
                    'nodeId':int(nr['id']),
                    'nodeGuid':str(nr.get('node_guid') or ''),
                    'path':str(nr.get('virtual_path') or ''),
                    'bookmarkTitle':title,
                    'physicalName':storage or legacy,
                    'fileId':None,
                    'reason':'Bookmark record has no file_id relationship.',
                    'label':title or storage or legacy or f"Bookmark {nr['id']}"
                })
                continue

            if not fr:
                records_without_files.append({
                    'kind':'recordWithoutFile',
                    'nodeId':int(nr['id']),
                    'nodeGuid':str(nr.get('node_guid') or ''),
                    'path':str(nr.get('virtual_path') or ''),
                    'bookmarkTitle':title,
                    'physicalName':storage or legacy,
                    'fileId':int(fid),
                    'reason':'Bookmark points to a file record that does not exist.',
                    'label':title or storage or legacy or f"Bookmark {nr['id']}"
                })
                continue

            if not storage or storage not in physical:
                records_without_files.append({
                    'kind':'recordWithoutFile',
                    'nodeId':int(nr['id']),
                    'nodeGuid':str(nr.get('node_guid') or ''),
                    'path':str(nr.get('virtual_path') or ''),
                    'bookmarkTitle':title,
                    'physicalName':storage,
                    'fileId':int(fid),
                    'fileGuid':str(fr.get('file_guid') or ''),
                    'reason':'SQLite record points to a physical file that is missing.',
                    'label':title or storage or f"Bookmark {nr['id']}"
                })

            if legacy != storage:
                metadata_issues.append({
                    'kind':'mismatchedNames',
                    'nodeId':int(nr['id']),
                    'nodeGuid':str(nr.get('node_guid') or ''),
                    'path':str(nr.get('virtual_path') or ''),
                    'bookmarkTitle':title,
                    'fileId':int(fid),
                    'physicalName':legacy,
                    'storageName':storage,
                    'label':title or storage or legacy,
                    'message':'Bookmark compatibility filename differs from the files table.'
                })

        # 2) File-table rows with no bookmark.
        # A legacy delete may have left a files-table row behind while the
        # physical payload is correctly held in an active Undo/Redo store.
        # That is intentional history state, not a damaged live library.
        history_owned=_history_owned_physical_names()
        for fr in file_rows:
            fid=int(fr['id'])
            if fid in nodes_by_file_id:
                continue

            name=str(fr.get('storage_name') or '')
            physical_exists=bool(name and name in physical)

            if not physical_exists and name in history_owned:
                continue

            if physical_exists:
                # Physical file exists, but the library has no bookmark owning it.
                files_without_records.append({
                    'kind':'fileRowWithoutBookmark',
                    'fileId':fid,
                    'fileGuid':str(fr.get('file_guid') or ''),
                    'physicalName':name,
                    'physicalExists':True,
                    'label':name or f'File {fid}',
                    'reason':'Physical file exists, but no StashLibrary bookmark is linked to its SQLite file record.'
                })
            else:
                # This is not a "file without a record": there is no physical file.
                # It is a stale SQLite record whose file is missing.
                records_without_files.append({
                    'kind':'fileRecordWithoutPhysical',
                    'fileId':fid,
                    'fileGuid':str(fr.get('file_guid') or ''),
                    'physicalName':name,
                    'physicalExists':False,
                    'bookmarkTitle':'',
                    'label':name or f'File {fid}',
                    'reason':'SQLite file record exists, but its physical file is missing.'
                })

        # 3) Physical archive files with no SQLite file record at all.
        for name,meta in physical.items():
            if name not in files_by_name:
                files_without_records.append({
                    'kind':'fileWithoutRecord',
                    'physicalName':name,
                    'size':meta['size'],
                    'ext':meta['ext'],
                    'physicalExists':True,
                    'label':name,
                    'reason':'Physical archive file exists, but there is no SQLite file record/bookmark for it.'
                })

        issues=records_without_files+files_without_records+metadata_issues
        summary={
            'recordsWithoutFiles':len(records_without_files),
            'filesWithoutRecords':len(files_without_records),
            'metadataIssues':len(metadata_issues)
        }

        return {
            'archiveFiles':len(physical),
            'bookmarkRows':len(node_rows),
            'fileRows':len(file_rows),
            'recordsWithoutFiles':records_without_files,
            'filesWithoutRecords':files_without_records,
            'metadataIssues':metadata_issues,
            'issues':issues,
            'summary':summary
        }

    return _with_db(op)


def repair_apply(issue):
    """Apply one explicit repair chosen by the user."""
    if not isinstance(issue,dict):
        raise RuntimeError('Invalid repair request.')
    kind=str(issue.get('kind') or '')
    action=str(issue.get('action') or '')

    def op(conn):
        arc=flat_archive_dir()

        # Match a bookmark record to a physical file chosen by the user.
        if action=='matchRecordToFile':
            nid=int(issue.get('nodeId'))
            chosen=str(issue.get('matchPhysicalName') or '')
            if not chosen:
                raise RuntimeError('Choose a physical file first.')
            p=arc/chosen
            if not p.is_file():
                raise RuntimeError(f'Physical file no longer exists: {chosen}')

            row=conn.execute("SELECT * FROM nodes WHERE id=?",(nid,)).fetchone()
            if not row:
                raise RuntimeError('Bookmark record no longer exists.')

            fid=_db_register_file(conn,chosen,row['created_at'])
            conn.execute(
                "UPDATE nodes SET file_id=?,physical_name=?,modified_at=? WHERE id=?",
                (fid,chosen,capture_timestamp(),nid)
            )
            _flat_write_recovery(conn)
            return {'action':'matchedRecordToFile','nodeId':nid,'physicalName':chosen}

        # Delete a stale SQLite file record whose physical file is gone
        # and which is not referenced by any bookmark.
        if action=='deleteStaleFileRecord':
            fid=int(issue.get('fileId'))
            refs=conn.execute("SELECT COUNT(*) n FROM nodes WHERE file_id=?",(fid,)).fetchone()['n']
            if refs:
                raise RuntimeError('File record is still linked to a bookmark.')
            conn.execute("DELETE FROM files WHERE id=?",(fid,))
            _flat_write_recovery(conn)
            return {'action':'deletedStaleFileRecord','fileId':fid}

        # Delete bookmark record only; physical file is left untouched.
        if action=='deleteRecord':
            nid=int(issue.get('nodeId'))
            row=conn.execute("SELECT * FROM nodes WHERE id=?",(nid,)).fetchone()
            if not row:
                return {'action':'recordAlreadyGone','nodeId':nid}
            parent_id=row['parent_id']
            conn.execute("DELETE FROM nodes WHERE id=?",(nid,))
            if parent_id is not None:
                _flat_reindex(conn,int(parent_id))
            _flat_write_recovery(conn)
            return {'action':'deletedRecord','nodeId':nid}

        # Delete a rogue physical file from archive.
        if action=='deletePhysicalFile':
            name=str(issue.get('physicalName') or '')
            if not name:
                raise RuntimeError('No physical filename supplied.')
            p=arc/name
            if p.is_file():
                p.unlink()
            # Remove unreferenced file record for this exact name if one exists.
            fr=conn.execute("SELECT id FROM files WHERE storage_name=?",(name,)).fetchone()
            if fr:
                fid=int(fr['id'])
                refs=conn.execute("SELECT COUNT(*) n FROM nodes WHERE file_id=?",(fid,)).fetchone()['n']
                if not refs:
                    conn.execute("DELETE FROM files WHERE id=?",(fid,))
            _flat_write_recovery(conn)
            return {'action':'deletedPhysicalFile','physicalName':name}

        # Turn an orphan physical file into a new root-level bookmark.
        if action=='createBookmarkForFile':
            name=str(issue.get('physicalName') or '')
            p=arc/name
            if not p.is_file():
                raise RuntimeError(f'Physical file no longer exists: {name}')
            root_id=_db_root_id(conn)
            title=Path(name).stem
            fid=_db_register_file(conn,name,capture_timestamp())
            nid=_flat_insert_bookmark(
                conn,root_id,name,title,'','',
                {'repairImportedOrphan':True}
            )
            conn.execute("UPDATE nodes SET file_id=? WHERE id=?",(fid,int(nid)))
            _flat_write_recovery(conn)
            return {'action':'createdBookmark','nodeId':int(nid),'title':title,'physicalName':name}

        # Delete a file-row only if nothing references it.
        if action=='deleteFileRecord':
            fid=int(issue.get('fileId'))
            refs=conn.execute("SELECT COUNT(*) n FROM nodes WHERE file_id=?",(fid,)).fetchone()['n']
            if refs:
                raise RuntimeError('File record is still linked to a bookmark.')
            conn.execute("DELETE FROM files WHERE id=?",(fid,))
            _flat_write_recovery(conn)
            return {'action':'deletedFileRecord','fileId':fid}

        # Metadata-only fix.
        if action=='syncMetadata' or kind=='mismatchedNames':
            nid=int(issue.get('nodeId'))
            fid=int(issue.get('fileId'))
            fr=conn.execute("SELECT * FROM files WHERE id=?",(fid,)).fetchone()
            if not fr:
                raise RuntimeError('File record no longer exists.')
            storage=str(fr['storage_name'] or '')
            conn.execute(
                "UPDATE nodes SET physical_name=?,modified_at=? WHERE id=?",
                (storage,capture_timestamp(),nid)
            )
            _flat_write_recovery(conn)
            return {'action':'syncedMetadata','nodeId':nid,'physicalName':storage}

        raise RuntimeError(f'Unsupported repair action: {action or kind}')

    return _with_db(op)

def repair_rescan_resync():
    """Re-read database/archive state and refresh recovery metadata.

    This intentionally does not make destructive guesses. It synchronizes safe
    metadata, writes a fresh recovery mirror and returns the rebuilt tree.
    """
    if not is_flat_layout():
        return {'tree':_database_tree(),'scan':repair_scan()}

    def op(conn):
        # First reconcile safe filename-only changes made outside StashLibrary.
        external_renames=_flat_reconcile_external_file_renames(conn)

        # Safe metadata resync: where file_id points to a valid files row, mirror
        # storage_name back into the compatibility physical_name field.
        rows=conn.execute(
            "SELECT n.id,n.physical_name,n.file_id,f.storage_name "
            "FROM nodes n LEFT JOIN files f ON f.id=n.file_id "
            "WHERE n.kind='bookmark'"
        ).fetchall()

        synced=0
        for row in rows:
            storage=str(row['storage_name'] or '')
            legacy=str(row['physical_name'] or '')
            if storage and storage!=legacy:
                conn.execute(
                    "UPDATE nodes SET physical_name=?,modified_at=? WHERE id=?",
                    (storage,capture_timestamp(),int(row['id']))
                )
                synced+=1

        # Clean up dead file-table rows left by older StashLibrary delete/undo code.
        # If no live bookmark references a row and its physical payload is not
        # in the live archive, the row contains no unique user data. Undo uses
        # its own snapshot/storage and can recreate the file row if restored.
        stale_removed=0
        for fr in conn.execute("SELECT id,storage_name FROM files").fetchall():
            fid=int(fr['id'])
            refs=conn.execute("SELECT COUNT(*) n FROM nodes WHERE file_id=?",(fid,)).fetchone()['n']
            if refs:
                continue
            storage=str(fr['storage_name'] or '')
            if storage and (flat_archive_dir()/storage).is_file():
                continue
            conn.execute("DELETE FROM files WHERE id=?",(fid,))
            stale_removed+=1

        _flat_write_recovery(conn)
        return {
            'synced': synced,
            'externalRenames': len(external_renames),
            'staleFileRecordsRemoved': stale_removed,
        }

    result=_with_db(op)

    # Clear readable localhost route cache so all future opens resolve from fresh
    # database state rather than a previously cached path.
    with FRIENDLY_HTTP_LOCK:
        FRIENDLY_ROUTES.clear()

    tree=_database_tree()
    scan=repair_scan()
    return {'synced':int(result.get('synced') or 0),'externalRenames':int(result.get('externalRenames') or 0),'staleFileRecordsRemoved':int(result.get('staleFileRecordsRemoved') or 0),'tree':tree,'scan':scan}


def database_info():
    root=bookmarks_path()
    if not root:return {'configured':False}
    def op(conn):
        count=conn.execute("SELECT COUNT(*) n FROM nodes WHERE kind!='root'").fetchone()['n']
        folders=conn.execute("SELECT COUNT(*) n FROM nodes WHERE kind='folder'").fetchone()['n']
        bookmarks=conn.execute("SELECT COUNT(*) n FROM nodes WHERE kind='bookmark'").fetchone()['n']
        integrity=conn.execute("PRAGMA quick_check").fetchone()[0]
        recovery_count=_flat_recovery_node_count() if is_flat_layout() else 0
        mismatch=bool(is_flat_layout() and recovery_count>int(count))
        files=conn.execute("SELECT COUNT(*) n FROM files").fetchone()['n']
        orphan_files=conn.execute(
            "SELECT COUNT(*) n FROM files f LEFT JOIN nodes n ON n.file_id=f.id WHERE n.id IS NULL"
        ).fetchone()['n']
        return {
            'configured':True,'path':str(database_file(root)),
            'internalDataPath':str(internal_data_dir(root=root) or ''),
            'archivePath':str(flat_archive_dir() if is_flat_layout() else root),
            'separatedInternalData':uses_separated_internal_data(),
            'nodes':int(count),'folders':int(folders),'bookmarks':int(bookmarks),
            'files':int(files),'orphanFileRecords':int(orphan_files),
            'schemaVersion':DATABASE_SCHEMA_VERSION,
            'storageModel':'places-like-nodes+zotero-like-files',
            'integrity':str(integrity),
            'recoveryNodes':int(recovery_count),'recoveryMismatch':mismatch
        }
    return _with_db(op)

def rebuild_database_from_recovery():
    root=bookmarks_path()
    if not root:raise RuntimeError('Choose a bookmarks folder first.')
    db=database_file(root)
    with DB_LOCK:
        for suffix in ('','-wal','-shm'):
            p=Path(str(db)+suffix)
            if p.exists():
                if suffix=='':
                    archived=db.with_name(f'stashlibrary-before-rebuild-{datetime.now().strftime("%Y%m%d-%H%M%S")}.sqlite3')
                    shutil.copy2(p,archived)
                p.unlink()
        conn=_db_connect()
        try:
            _db_import_folder(conn,Path(root),_db_root_id(conn))
            conn.execute("INSERT OR REPLACE INTO catalogue_meta(key,value) VALUES('initial_import_complete',?)",(capture_timestamp(),))
            conn.commit()
        finally:conn.close()
    return database_info()


FLAT_ARCHIVE_DIR_NAME='archive'
FLAT_RECOVERY_NAME='catalogue-recovery.json'
VIRTUAL_ROOT='@STASHLIBRARY'
LEGACY_VIRTUAL_ROOT='@STASHLIBRARY'

def _normalize_virtual_ref(ref):
    """Accept persisted pre-rebrand IDs while emitting StashLibrary IDs."""
    value=str(ref or '')
    if value==LEGACY_VIRTUAL_ROOT:return VIRTUAL_ROOT
    if value.startswith(LEGACY_VIRTUAL_ROOT+'/'):
        return VIRTUAL_ROOT+value[len(LEGACY_VIRTUAL_ROOT):]
    return value

def flat_archive_dir():
    root=bookmarks_path()
    if not root:return None
    # Flat-layout HTML/PDF files live directly in the visible StashLibrary folder.
    # Internal catalogue/recovery data may be in AppData on older installs or in
    # the storage folder's hidden .stashlibrary-data directory from v0.10.161.
    if uses_separated_internal_data():
        p=Path(root)
    else:
        p=Path(root)/SYSTEM_DIR_NAME/FLAT_ARCHIVE_DIR_NAME
    p.mkdir(parents=True,exist_ok=True)
    return p

def flat_recovery_file():
    p=internal_data_dir(create=True)
    return (p/FLAT_RECOVERY_NAME) if p else None

def is_flat_layout():
    return read_config().get('storage_layout') in {'flat-sqlite-v1','flat-sqlite-v2'}

def _virtual_ref_from_ids(ids):
    return VIRTUAL_ROOT + (('/'+'/'.join(str(x) for x in ids)) if ids else '')

def _node_id_from_ref(ref):
    s=_normalize_virtual_ref(ref)
    if s==VIRTUAL_ROOT:return 1
    if not s.startswith(VIRTUAL_ROOT+'/'):
        return None
    tail=s[len(VIRTUAL_ROOT)+1:].split('/')[-1]
    try:return int(tail)
    except:return None

def _flat_row(conn,ref):
    nid=_node_id_from_ref(ref)
    if nid is None:return None
    return conn.execute("SELECT * FROM nodes WHERE id=?",(nid,)).fetchone()

def _flat_ref_for_id(conn,node_id):
    node_id=int(node_id)
    ids=[]
    cur=conn.execute("SELECT id,parent_id FROM nodes WHERE id=?",(node_id,)).fetchone()
    while cur and int(cur['id'])!=_db_root_id(conn):
        ids.append(int(cur['id']))
        pid=cur['parent_id']
        if pid is None:break
        cur=conn.execute("SELECT id,parent_id FROM nodes WHERE id=?",(int(pid),)).fetchone()
    return _virtual_ref_from_ids(reversed(ids))

def _flat_payload_path_from_row(row):
    if not row or row['kind']!='bookmark':return None
    return flat_archive_dir()/str(row['physical_name'])

def _flat_payload_path(ref):
    def op(conn):
        row=_flat_row(conn,ref)
        if not row or row['kind']!='bookmark':
            raise RuntimeError('This StashLibrary entry is not a file/bookmark.')
        p=_flat_payload_path_from_row(row)
        if not p.exists():
            raise RuntimeError(f'Archived file is missing: {p.name}')
        return p
    return _with_db(op)

def _flat_reserved_archive_stems():
    reserved=set()
    for a in list(undo_stack)+list(redo_stack):
        for name in a.get('flatPhysicalNames') or []:
            reserved.add(Path(str(name)).stem.lower())
    return reserved

FLAT_FILENAME_TARGET_PATH=240

def _safe_physical_stem(title):
    """Return a readable Windows-safe archive stem.

    The full display title remains in SQLite; this only controls the physical
    filename used in StashLibrary's flat archive.
    """
    s=normalized_display_title(title or 'document').strip() or 'document'
    s=re.sub(r'[<>:"/\\|?*\x00-\x1f]+',' - ',s)
    s=re.sub(r'\s+',' ',s).strip(' .')
    if not s:s='document'

    # Avoid Windows device names, even when followed by an extension.
    reserved={'CON','PRN','AUX','NUL'}
    reserved|={f'COM{i}' for i in range(1,10)}
    reserved|={f'LPT{i}' for i in range(1,10)}
    if s.upper() in reserved:
        s='_'+s

    return s

def _fit_flat_filename(stem,ext,suffix_text=''):
    """Fit a readable filename under a conservative full-path target."""
    arc=flat_archive_dir()
    ext=str(ext or '')
    if ext and not ext.startswith('.'):ext='.'+ext

    stem=_safe_physical_stem(stem)
    suffix_text=str(suffix_text or '')
    # Keep enough room for archive path + separator + extension + collision.
    available=FLAT_FILENAME_TARGET_PATH-len(str(arc))-1-len(ext)-len(suffix_text)
    available=max(24,available)

    if len(stem)>available:
        stem=stem[:available].rstrip(' .-_')
    if not stem:stem='document'
    return f'{stem}{suffix_text}{ext}'

def flat_archive_dest(ext='',title='document',node_hint=None,ignore_path=None):
    """Allocate a human-readable filename in the flat archive.

    The first file uses the clean title. Collisions become:
      Title.pdf
      Title (2).pdf
      Title (3).pdf

    SQLite remains the real identity system, so collision numbers are purely a
    physical-filesystem detail.
    """
    arc=flat_archive_dir()
    if not arc:raise RuntimeError('Choose a bookmarks folder first.')
    ext=str(ext or '')
    if ext and not ext.startswith('.'):ext='.'+ext

    ignore=Path(ignore_path).resolve() if ignore_path else None
    reserved_names=set()
    for p in arc.iterdir():
        if not p.is_file():continue
        try:
            if ignore and p.resolve()==ignore:
                continue
        except Exception:
            pass
        reserved_names.add(p.name.lower())

    for a in list(undo_stack)+list(redo_stack):
        for name in a.get('flatPhysicalNames') or []:
            reserved_names.add(str(name).lower())

    base=_safe_physical_stem(title)
    candidate_name=_fit_flat_filename(base,ext)
    if candidate_name.lower() not in reserved_names and not (arc/candidate_name).exists():
        return arc/candidate_name

    i=2
    while True:
        suffix=f' ({i})'
        candidate_name=_fit_flat_filename(base,ext,suffix)
        candidate=arc/candidate_name
        if candidate_name.lower() not in reserved_names and not candidate.exists():
            return candidate
        i+=1

def _make_flat_filenames_readable():
    """Rename existing flat payloads to their StashLibrary display titles.

    Only the physical archive filename changes. SQLite IDs, display names,
    hierarchy, URLs, Accessed dates and file contents are preserved.
    """
    if not is_flat_layout():
        return {'renamed':0}

    def op(conn):
        rows=conn.execute("SELECT * FROM nodes WHERE kind='bookmark' ORDER BY id").fetchall()
        renamed=0

        # Rename through temporary names first. This avoids one existing numeric
        # filename blocking another item's desired readable name during migration.
        staged=[]
        for row in rows:
            old=flat_archive_dir()/str(row['physical_name'])
            if not old.exists():continue
            temp=flat_archive_dir()/f'.stashlibrary-rename-{uuid.uuid4().hex}{old.suffix}'
            old.rename(temp)
            staged.append((row,temp))

        try:
            for row,temp in staged:
                target=flat_archive_dest(
                    temp.suffix,
                    str(row['display_name'] or temp.stem),
                    row['id']
                )
                temp.rename(target)
                conn.execute(
                    "UPDATE nodes SET physical_name=? WHERE id=?",
                    (target.name,int(row['id']))
                )
                renamed+=1
            _flat_write_recovery(conn)
        except Exception:
            # Best-effort: leave staged files recoverable rather than deleting data.
            raise

        return renamed

    renamed=_with_db(op)
    cfg=read_config()
    cfg['flat_readable_filenames']='v1-collision-numbers'
    cfg.pop('flat_random_ids',None)
    cfg.pop('friendly_flat_filenames',None)
    write_config(cfg)
    return {'renamed':renamed}

def _ensure_readable_flat_filenames():
    if not is_flat_layout():
        return
    cfg=read_config()
    if cfg.get('flat_readable_filenames')=='v1-collision-numbers':
        return
    _make_flat_filenames_readable()



def _db_file_for_node(conn,row_or_id):
    if isinstance(row_or_id,(int,str)) and str(row_or_id).isdigit():
        row=conn.execute("SELECT file_id FROM nodes WHERE id=?",(int(row_or_id),)).fetchone()
    else:
        row=row_or_id
    if not row or row['file_id'] is None:
        return None
    return conn.execute("SELECT * FROM files WHERE id=?",(int(row['file_id']),)).fetchone()


def _filesystem_identity(path):
    """Return the OS/filesystem identity exposed by Python's os.stat().

    st_dev + st_ino is cross-platform at the Python API level. Ordinary renames
    on the same filesystem keep this identity, while a copied file gets a new one.
    """
    p=Path(path)
    st=p.stat()
    return str(getattr(st,'st_dev','') or ''),str(getattr(st,'st_ino','') or '')


def _file_sha256(path,chunk_size=1024*1024):
    """Return a content identity for a physical archive file."""
    p=Path(path)
    h=hashlib.sha256()
    with p.open('rb') as f:
        while True:
            chunk=f.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _flat_refresh_file_identity(conn,file_id,storage_name,force_hash=False):
    """Refresh physical identity and fallback integrity metadata."""
    arc=flat_archive_dir()
    if not arc:
        return None
    p=arc/str(storage_name or '')
    if not p.is_file():
        return None

    row=conn.execute(
        "SELECT id,size_bytes,sha256,fs_dev,fs_ino FROM files WHERE id=?",(int(file_id),)
    ).fetchone()
    if not row:
        return None

    try:
        size=int(p.stat().st_size)
    except Exception:
        size=0

    try:
        fs_dev,fs_ino=_filesystem_identity(p)
    except Exception:
        fs_dev,fs_ino='',''

    digest=str(row['sha256'] or '')
    if force_hash or not digest:
        try:digest=_file_sha256(p)
        except Exception:digest=''

    conn.execute(
        "UPDATE files SET size_bytes=?,sha256=?,fs_dev=?,fs_ino=?,extension=?,modified_at=? WHERE id=?",
        (size,digest,fs_dev,fs_ino,p.suffix.lower(),capture_timestamp(),int(file_id))
    )
    return {'size':size,'sha256':digest,'fs_dev':fs_dev,'fs_ino':fs_ino}


def _flat_seed_file_identities(conn):
    """Backfill filesystem identity for healthy physical file records."""
    arc=flat_archive_dir()
    if not arc or not arc.exists():
        return 0

    seeded=0
    rows=conn.execute(
        "SELECT id,storage_name,fs_dev,fs_ino FROM files ORDER BY id"
    ).fetchall()
    for row in rows:
        name=str(row['storage_name'] or '')
        if not name:
            continue
        p=arc/name
        if not p.is_file():
            continue
        if str(row['fs_dev'] or '') and str(row['fs_ino'] or ''):
            continue
        _flat_refresh_file_identity(conn,int(row['id']),name,force_hash=False)
        seeded+=1
    return seeded


def _flat_reconcile_external_file_renames(conn):
    """Reconcile external renames using cross-platform filesystem identity.

    Primary identity: Python os.stat() st_dev + st_ino.
    Fallback: SHA-256 only when filesystem identity is unavailable.
    """
    arc=flat_archive_dir()
    if not arc or not arc.exists():
        return []

    _flat_seed_file_identities(conn)

    physical={}
    for p in arc.iterdir():
        if not p.is_file():
            continue
        try:
            dev,ino=_filesystem_identity(p)
        except Exception:
            dev,ino='',''
        physical[p.name]={'path':p,'dev':dev,'ino':ino}

    rows=conn.execute(
        "SELECT id,storage_name,size_bytes,sha256,fs_dev,fs_ino FROM files ORDER BY id"
    ).fetchall()
    registered={str(r['storage_name'] or '') for r in rows if r['storage_name']}
    missing=[r for r in rows if r['storage_name'] and str(r['storage_name']) not in physical]
    unregistered=[v for name,v in physical.items() if name not in registered]

    if not missing or not unregistered:
        return []

    by_fs={}
    for item in unregistered:
        if item['dev'] and item['ino']:
            by_fs.setdefault((item['dev'],item['ino']),[]).append(item)

    renamed=[]
    used=set()

    # Exact filesystem identity: no byte-duplicate ambiguity.
    for row in missing:
        key=(str(row['fs_dev'] or ''),str(row['fs_ino'] or ''))
        if not all(key):
            continue
        matches=[x for x in by_fs.get(key,[]) if x['path'].name not in used]
        if len(matches)==1:
            item=matches[0]
            old=str(row['storage_name'])
            _db_set_file_storage_name(conn,int(row['id']),item['path'].name)
            _flat_refresh_file_identity(conn,int(row['id']),item['path'].name)
            used.add(item['path'].name)
            renamed.append({'fileId':int(row['id']),'oldName':old,'newName':item['path'].name,'matchedBy':'filesystem-id'})

    # Fallback only for records/filesystems that did not expose a usable identity.
    still_missing=[
        r for r in missing
        if int(r['id']) not in {x['fileId'] for x in renamed}
        and str(r['sha256'] or '')
    ]
    remaining=[x for x in unregistered if x['path'].name not in used]
    if still_missing and remaining:
        needed_sizes={int(r['size_bytes'] or 0) for r in still_missing}
        candidate_info=[]
        for item in remaining:
            p=item['path']
            try:size=int(p.stat().st_size)
            except Exception:continue
            if needed_sizes and size not in needed_sizes:
                continue
            try:digest=_file_sha256(p)
            except Exception:continue
            candidate_info.append((p,size,digest))

        for row in still_missing:
            size=int(row['size_bytes'] or 0)
            digest=str(row['sha256'] or '')
            matches=[p for p,csize,chash in candidate_info if p.name not in used and csize==size and chash==digest]
            if len(matches)==1:
                p=matches[0]
                old=str(row['storage_name'])
                _db_set_file_storage_name(conn,int(row['id']),p.name)
                _flat_refresh_file_identity(conn,int(row['id']),p.name)
                used.add(p.name)
                renamed.append({'fileId':int(row['id']),'oldName':old,'newName':p.name,'matchedBy':'sha256-fallback'})

    if renamed:
        _flat_write_recovery(conn)
        with FRIENDLY_HTTP_LOCK:
            FRIENDLY_ROUTES.clear()

    return renamed


def _db_register_file(conn,storage_name,created_at=None):
    storage_name=str(storage_name or '')
    if not storage_name:
        raise RuntimeError('Cannot register an empty physical filename.')
    existing=conn.execute("SELECT * FROM files WHERE storage_name=?",(storage_name,)).fetchone()
    if existing:
        return int(existing['id'])
    now=capture_timestamp()
    p=flat_archive_dir()/storage_name
    try:size=int(p.stat().st_size) if p.is_file() else 0
    except Exception:size=0
    digest=''
    try:
        if p.is_file():
            digest=_file_sha256(p)
    except Exception:
        digest=''
    try:
        fs_dev,fs_ino=_filesystem_identity(p) if p.is_file() else ('','')
    except Exception:
        fs_dev,fs_ino='',''
    cur=conn.execute(
        "INSERT INTO files(file_guid,storage_name,extension,size_bytes,sha256,fs_dev,fs_ino,created_at,modified_at) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        (_stable_guid('f_'),storage_name,Path(storage_name).suffix.lower(),size,digest,fs_dev,fs_ino,
         str(created_at or now),now)
    )
    return int(cur.lastrowid)

def _db_set_file_storage_name(conn,file_id,new_name):
    now=capture_timestamp()
    conn.execute(
        "UPDATE files SET storage_name=?,extension=?,modified_at=? WHERE id=?",
        (str(new_name),Path(str(new_name)).suffix.lower(),now,int(file_id))
    )
    # Compatibility mirror for older callers and recovery snapshots.
    conn.execute(
        "UPDATE nodes SET physical_name=?,modified_at=? WHERE file_id=?",
        (str(new_name),now,int(file_id))
    )

def _db_storage_name_for_row(conn,row):
    if row and row['file_id'] is not None:
        f=conn.execute("SELECT storage_name FROM files WHERE id=?",(int(row['file_id']),)).fetchone()
        if f and f['storage_name']:
            return str(f['storage_name'])
    return str(row['physical_name'] or '') if row else ''


def _flat_children(conn,parent_id):
    return conn.execute(
        "SELECT * FROM nodes WHERE parent_id=? ORDER BY position,id",(int(parent_id),)
    ).fetchall()

def _flat_reindex(conn,parent_id):
    rows=_flat_children(conn,parent_id)
    for i,r in enumerate(rows):
        conn.execute("UPDATE nodes SET position=? WHERE id=?",(i,int(r['id'])))

def _flat_extra(row):
    try:
        x=json.loads(row['extra_json'] or '{}')
        return x if isinstance(x,dict) else {}
    except:return {}

def _flat_write_recovery(conn):
    f=flat_recovery_file()
    if not f:return
    rows=conn.execute(
        "SELECT id,parent_id,physical_name,kind,display_name,source_url,accessed_at,position,extra_json,created_at,node_guid,file_id,modified_at "
        "FROM nodes ORDER BY id"
    ).fetchall()
    files=conn.execute(
        "SELECT id,file_guid,storage_name,extension,size_bytes,sha256,created_at,modified_at "
        "FROM files ORDER BY id"
    ).fetchall()
    payload={
        'format':'stashlibrary-flat-recovery-v2',
        'schemaVersion':DATABASE_SCHEMA_VERSION,
        'storageModel':'places-like-nodes+zotero-like-files',
        'savedAt':capture_timestamp(),
        'nodes':[dict(r) for r in rows],
        'files':[dict(r) for r in files]
    }
    tmp=f.with_suffix('.tmp')
    tmp.write_text(json.dumps(payload,indent=2,ensure_ascii=False),encoding='utf-8')
    tmp.replace(f)

def _flat_insert_folder(conn,parent_id,name,extra=None,position=None,explicit_id=None):
    if position is None:
        position=conn.execute("SELECT COALESCE(MAX(position),-1)+1 n FROM nodes WHERE parent_id=?",(parent_id,)).fetchone()['n']
    created=capture_timestamp()
    extra=dict(extra or {})
    guid=_stable_guid('n_')
    if explicit_id is None:
        cur=conn.execute(
            "INSERT INTO nodes(parent_id,physical_name,kind,display_name,position,extra_json,created_at,node_guid,modified_at) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (parent_id,'pending','folder',str(name or 'New folder'),int(position),
             json.dumps(extra,ensure_ascii=False),created,guid,created)
        )
        nid=int(cur.lastrowid)
    else:
        nid=int(explicit_id)
        conn.execute(
            "INSERT INTO nodes(id,parent_id,physical_name,kind,display_name,position,extra_json,created_at,node_guid,modified_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (nid,parent_id,f'f{nid}','folder',str(name or 'New folder'),int(position),
             json.dumps(extra,ensure_ascii=False),created,guid,created)
        )
        return nid
    conn.execute("UPDATE nodes SET physical_name=? WHERE id=?",(f'f{nid}',nid))
    return nid

def _flat_insert_bookmark(conn,parent_id,physical_name,title,source_url='',accessed_at='',extra=None,position=None,explicit_id=None):
    if position is None:
        position=conn.execute("SELECT COALESCE(MAX(position),-1)+1 n FROM nodes WHERE parent_id=?",(parent_id,)).fetchone()['n']
    created=capture_timestamp()
    storage=str(physical_name)
    file_id=_db_register_file(conn,storage,created)
    vals=(parent_id,storage,'bookmark',str(title or Path(storage).stem),
          str(source_url or ''),str(accessed_at or ''),int(position),
          json.dumps(dict(extra or {}),ensure_ascii=False),created,
          _stable_guid('n_'),file_id,created)
    if explicit_id is None:
        cur=conn.execute(
            "INSERT INTO nodes(parent_id,physical_name,kind,display_name,source_url,accessed_at,position,extra_json,created_at,node_guid,file_id,modified_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",vals
        )
        return int(cur.lastrowid)
    conn.execute(
        "INSERT INTO nodes(id,parent_id,physical_name,kind,display_name,source_url,accessed_at,position,extra_json,created_at,node_guid,file_id,modified_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",(int(explicit_id),)+vals
    )
    return int(explicit_id)

def _flat_snapshot_subtree(conn,node_id):
    out=[]
    def walk(nid):
        row=conn.execute("SELECT * FROM nodes WHERE id=?",(int(nid),)).fetchone()
        if not row:return
        out.append(dict(row))
        for ch in _flat_children(conn,nid):
            walk(int(ch['id']))
    walk(int(node_id))
    return out

def _flat_delete_unreferenced_file_rows(conn, rows):
    """Remove file-table rows that are no longer owned by any live bookmark.

    Undo snapshots keep the bookmark metadata and physical filenames, so a
    deleted file row does not need to remain in the live catalogue.  Undo will
    recreate/register the file row when the bookmark is restored.
    """
    candidates=set()
    for row in rows or []:
        if str(row.get('kind') or '')!='bookmark':
            continue
        fid=row.get('file_id')
        if fid is not None:
            candidates.add(int(fid))
        storage=str(row.get('physical_name') or '')
        if storage:
            fr=conn.execute("SELECT id FROM files WHERE storage_name=?",(storage,)).fetchone()
            if fr:
                candidates.add(int(fr['id']))

    removed=0
    for fid in candidates:
        refs=conn.execute("SELECT COUNT(*) n FROM nodes WHERE file_id=?",(fid,)).fetchone()['n']
        if not refs:
            conn.execute("DELETE FROM files WHERE id=?",(fid,))
            removed+=1
    return removed

def _history_owned_physical_names():
    """Physical filenames deliberately held by current Undo/Redo actions."""
    names=set()
    for action in list(undo_stack)+list(redo_stack):
        for name in action.get('flatPhysicalNames') or []:
            if name:
                names.add(str(name))
    return names

def _repair_restored_bookmark_file_links(conn, rows):
    """Normalize restored bookmarks into the schema-v2 file model.

    Handles legacy v0.9.x undo snapshots that only contain physical_name and
    have no file_id/files-table record.
    """
    repaired=0
    for original in rows:
        if str(original.get('kind') or '')!='bookmark':
            continue

        nid=int(original['id'])
        row=conn.execute(
            "SELECT id,physical_name,file_id,created_at FROM nodes WHERE id=?",
            (nid,)
        ).fetchone()
        if not row:
            raise RuntimeError(f'Undo repair could not find restored bookmark row {nid}.')

        storage=str(row['physical_name'] or original.get('physical_name') or '')
        if not storage:
            raise RuntimeError(f'Undo repair found bookmark {nid} without a physical filename.')

        payload=flat_archive_dir()/storage
        if not payload.is_file():
            raise RuntimeError(
                f'Undo repair could not find restored file for bookmark {nid}: {storage}'
            )

        file_row=None
        if row['file_id'] is not None:
            file_row=conn.execute(
                "SELECT * FROM files WHERE id=?",
                (int(row['file_id']),)
            ).fetchone()

        if file_row and str(file_row['storage_name'] or '') != storage:
            matching=conn.execute(
                "SELECT * FROM files WHERE storage_name=?",
                (storage,)
            ).fetchone()
            file_row=matching if matching else None

        if not file_row:
            matching=conn.execute(
                "SELECT * FROM files WHERE storage_name=?",
                (storage,)
            ).fetchone()
            if matching:
                file_row=matching
            else:
                file_id=_db_register_file(
                    conn,
                    storage,
                    row['created_at'] or original.get('created_at')
                )
                file_row=conn.execute(
                    "SELECT * FROM files WHERE id=?",
                    (int(file_id),)
                ).fetchone()

        if not file_row:
            raise RuntimeError(
                f'Undo repair could not register restored file record for: {storage}'
            )

        file_id=int(file_row['id'])

        if str(file_row['storage_name']) != storage:
            _db_set_file_storage_name(conn,file_id,storage)

        conn.execute(
            "UPDATE nodes SET file_id=?,physical_name=?,modified_at=? WHERE id=?",
            (file_id,storage,capture_timestamp(),nid)
        )

        check=conn.execute(
            "SELECT n.file_id,n.physical_name,f.storage_name "
            "FROM nodes n LEFT JOIN files f ON f.id=n.file_id WHERE n.id=?",
            (nid,)
        ).fetchone()

        if (
            not check
            or check['file_id'] is None
            or not check['storage_name']
            or str(check['physical_name']) != str(check['storage_name'])
            or not (flat_archive_dir()/str(check['storage_name'])).is_file()
        ):
            raise RuntimeError(
                f'Undo repair left bookmark {nid} with an invalid file relationship.'
            )

        repaired+=1

    return repaired


def _flat_restore_snapshot(conn,rows):
    rows=[dict(x) for x in rows]
    pending={int(r['id']):r for r in rows}
    inserted=set()
    while pending:
        progressed=False
        for nid,row in list(pending.items()):
            pid=row.get('parent_id')
            if pid is None or conn.execute("SELECT 1 FROM nodes WHERE id=?",(pid,)).fetchone() or pid in inserted:
                file_id=row.get('file_id')
                if row.get('kind')=='bookmark':
                    storage=str(row.get('physical_name') or '')
                    if file_id is not None:
                        exists=conn.execute("SELECT 1 FROM files WHERE id=?",(int(file_id),)).fetchone()
                    else:
                        exists=None
                    if not exists:
                        file_id=_db_register_file(conn,storage,row.get('created_at'))
                else:
                    file_id=None

                conn.execute(
                    "INSERT INTO nodes(id,parent_id,physical_name,kind,display_name,source_url,accessed_at,position,extra_json,created_at,node_guid,file_id,modified_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (nid,pid,row.get('physical_name',''),row['kind'],row['display_name'],row.get('source_url',''),
                     row.get('accessed_at',''),row.get('position',0),row.get('extra_json','{}'),
                     row.get('created_at',''),row.get('node_guid') or _stable_guid('n_'),
                     file_id,row.get('modified_at') or capture_timestamp())
                )
                inserted.add(nid);pending.pop(nid);progressed=True
        if not progressed:
            raise RuntimeError('Could not restore virtual folder hierarchy.')
    _repair_restored_bookmark_file_links(conn,rows)

def _flat_create_folder(parent_ref,name,record=True):
    def op(conn):
        parent=_flat_row(conn,parent_ref)
        if not parent or parent['kind'] not in {'root','folder'}:raise RuntimeError('Destination folder no longer exists.')
        nid=_flat_insert_folder(conn,int(parent['id']),name)
        row=dict(conn.execute("SELECT * FROM nodes WHERE id=?",(nid,)).fetchone())
        _flat_write_recovery(conn)
        return nid,row
    nid,row=_with_db(op)
    ref=None
    def getref(conn):return _flat_ref_for_id(conn,nid)
    ref=_with_db(getref)
    if record:
        record_action({'type':'flat_created','label':f'Create folder {name}','rows':[row],'flatPhysicalNames':[]})
    return ref

def _flat_add_file_record(parent_ref,payload,title,source_url='',accessed_at='',extra=None,record=True):
    payload=Path(payload)
    def op(conn):
        parent=_flat_row(conn,parent_ref)
        if not parent or parent['kind'] not in {'root','folder'}:raise RuntimeError('Destination folder no longer exists.')
        nid=_flat_insert_bookmark(conn,int(parent['id']),payload.name,title,source_url,accessed_at,extra)
        row=dict(conn.execute("SELECT * FROM nodes WHERE id=?",(nid,)).fetchone())
        _flat_write_recovery(conn)
        return nid,row
    nid,row=_with_db(op)
    ref=_with_db(lambda conn:_flat_ref_for_id(conn,nid))
    if record:
        record_action({'type':'flat_created','label':f'Add {title}','rows':[row],'flatPhysicalNames':[payload.name]})
    return ref

def _flat_save_url(parent_ref,url,name):
    parsed=urllib.parse.urlparse(url)
    accessed=capture_timestamp()
    if parsed.scheme=='file':
        src=Path(urllib.request.url2pathname(parsed.path))
    elif parsed.scheme=='':
        src=Path(url)
    else:
        src=None
    if src is not None and src.exists():
        title=normalized_display_title(name or src.stem,src.suffix)
        dest=flat_archive_dest(src.suffix,title)
        copy_file_with_progress(src,dest,'Saving local file')
        return _flat_add_file_record(parent_ref,dest,title,url,accessed,{'originalFilename':src.name})
    if parsed.path.lower().endswith('.pdf'):
        title=normalized_display_title(name or get_title_from_url(url),'.pdf')
        dest=flat_archive_dest('.pdf',title)
        req=urllib.request.Request(url,headers={'User-Agent':'Mozilla/5.0'})
        with urllib.request.urlopen(req,timeout=60) as resp,open(dest,'wb') as f:
            shutil.copyfileobj(resp,f)
        original=Path(urllib.parse.unquote(parsed.path)).name
        return _flat_add_file_record(parent_ref,dest,title,url,accessed,{'originalFilename':original})
    raise RuntimeError('Normal webpages are saved through the SingleFile Firefox extension, not the native helper.')


def _flat_physical_name(ref):
    def op(conn):
        row=_flat_row(conn,ref)
        if not row or row['kind']!='bookmark':
            return ''
        file_id=row['file_id']
        if file_id is None:
            return ''
        fr=conn.execute("SELECT storage_name FROM files WHERE id=?",(int(file_id),)).fetchone()
        return str(fr['storage_name']) if fr and fr['storage_name'] else ''
    return _with_db(op)


def _flat_save_pdf_data(parent_ref,name,pdf_base64,display_title='',source_url=''):
    try:data=base64.b64decode(pdf_base64,validate=True)
    except Exception as e:raise RuntimeError('Firefox supplied unreadable PDF data.') from e
    if not data or b'%PDF-' not in data[:1024]:raise RuntimeError('The hosted document did not contain a valid PDF signature.')
    accessed=capture_timestamp()
    title=normalized_display_title(display_title or Path(name or 'document.pdf').stem,'.pdf')
    dest=flat_archive_dest('.pdf',title)
    dest.write_bytes(data)
    return _flat_add_file_record(parent_ref,dest,title,source_url,accessed,{'originalFilename':str(name or '')})


def _flat_import_download(src,parent_ref,display_title='',source_url=''):
    src=Path(src)
    if not src.exists():raise RuntimeError(f'Finished SingleFile download was not found: {src}')
    title=normalized_display_title(display_title or src.stem,src.suffix)
    dest=flat_archive_dest(src.suffix,title)
    original_name=src.name
    try:shutil.move(str(src),str(dest))
    except Exception:
        shutil.copy2(src,dest)
        try:src.unlink()
        except:pass
    accessed=capture_timestamp()
    return _flat_add_file_record(parent_ref,dest,title,source_url,accessed,{'originalFilename':original_name})


def _flat_rename(ref,new_name,record=True):
    """Rename only the title shown in StashLibrary/SQLite."""
    def op(conn):
        row=_flat_row(conn,ref)
        if not row:raise RuntimeError('Item no longer exists.')
        old=str(row['display_name'])
        new_display=str(new_name or '').strip()
        if not new_display:raise RuntimeError('Bookmark name cannot be empty.')
        conn.execute("UPDATE nodes SET display_name=?,modified_at=? WHERE id=?",(new_display,capture_timestamp(),int(row['id'])))
        _flat_write_recovery(conn)
        return old,int(row['id'])

    old,nid=_with_db(op)
    if record:
        record_action({
            'type':'flat_rename',
            'label':f'Rename Bookmark {old}',
            'nodeID':nid,
            'oldName':old,
            'newName':str(new_name).strip()
        })
    return ref

def _flat_rename_physical_file(ref,new_name):
    """Rename only the backing PDF/HTML file and update SQLite physical_name."""
    if not is_flat_layout():
        raise RuntimeError('Rename Physical File is only available in flat-archive mode.')

    def op(conn):
        row=_flat_row(conn,ref)
        if not row:raise RuntimeError('Bookmark no longer exists.')
        if row['kind']!='bookmark':
            raise RuntimeError('StashLibrary folders are virtual and do not have their own physical files.')

        old_path=_flat_payload_path_from_row(row)
        if not old_path.exists():
            raise RuntimeError(f'Archived file is missing: {old_path.name}')

        requested=str(new_name or '').strip()
        if not requested:raise RuntimeError('Physical filename cannot be empty.')

        current_ext=old_path.suffix
        typed=Path(requested)

        # Allow users to type either "My Paper" or "My Paper.pdf", but do not
        # silently change the underlying file type.
        if typed.suffix:
            if typed.suffix.lower()!=current_ext.lower():
                raise RuntimeError(f'Keep the existing file extension: {current_ext}')
            requested_stem=typed.stem
        else:
            requested_stem=requested

        target=flat_archive_dest(
            current_ext,
            requested_stem,
            row['id'],
            ignore_path=old_path
        )

        if target.name!=old_path.name:
            old_path.rename(target)
            file_id=row['file_id']
            if file_id is None:
                file_id=_db_register_file(conn,old_path.name,row['created_at'])
                conn.execute("UPDATE nodes SET file_id=? WHERE id=?",(file_id,int(row['id'])))
            _db_set_file_storage_name(conn,int(file_id),target.name)
            _flat_write_recovery(conn)

        return target.name

    return _with_db(op)


def _windows_explorer_select(path):
    """Open File Explorer and select/highlight an exact file."""
    p=Path(path)
    if sys.platform!='win32':
        # Non-Windows fallback: open parent folder.
        subprocess.Popen(['xdg-open',str(p.parent if p.is_file() else p)])
        return

    if p.is_file():
        # Explorer /select highlights the exact physical file.
        subprocess.Popen(
            ['explorer.exe', f'/select,{str(p)}'],
            creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0)
        )
    else:
        subprocess.Popen(
            ['explorer.exe', str(p)],
            creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0)
        )

def _flat_open_physical_location(ref):
    """Reveal the exact physical backing file for a bookmark.

    For virtual StashLibrary folders, open the flat archive folder because folders
    themselves do not have physical directory counterparts in schema-v2.
    """
    if not is_flat_layout():
        raise RuntimeError('Physical file location is only available in flat-archive mode.')

    def op(conn):
        row=_flat_row(conn,ref)
        if not row:
            raise RuntimeError('StashLibrary item no longer exists.')

        if row['kind']=='folder':
            return {'kind':'folder','path':str(flat_archive_dir())}

        storage=_db_storage_name_for_row(conn,row)
        if not storage:
            raise RuntimeError('Bookmark has no physical file relationship.')

        p=flat_archive_dir()/storage

        # If metadata is stale, try the legacy compatibility filename before failing.
        if not p.is_file():
            legacy=flat_archive_dir()/str(row['physical_name'] or '')
            if legacy.is_file():
                fid=row['file_id']
                if fid is None:
                    fid=_db_register_file(conn,legacy.name,row['created_at'])
                    conn.execute(
                        "UPDATE nodes SET file_id=?,physical_name=?,modified_at=? WHERE id=?",
                        (fid,legacy.name,capture_timestamp(),int(row['id']))
                    )
                else:
                    _db_set_file_storage_name(conn,int(fid),legacy.name)
                _flat_write_recovery(conn)
                p=legacy

        if not p.is_file():
            raise RuntimeError(f'Physical file no longer exists: {storage}')

        return {'kind':'bookmark','path':str(p)}

    info=_with_db(op)
    _windows_explorer_select(Path(info['path']))
    return info


def _flat_reorder(src_ref,parent_ref,target_ref=None,position='end',record=True):
    def op(conn):
        src=_flat_row(conn,src_ref);par=_flat_row(conn,parent_ref)
        if not src or not par:raise RuntimeError('Item or folder no longer exists.')
        pid=int(par['id'])
        if int(src['parent_id'])!=pid:raise RuntimeError('Item is not in that folder.')
        before=[int(r['id']) for r in _flat_children(conn,pid)]
        ids=[x for x in before if x!=int(src['id'])]
        if target_ref:
            trg=_flat_row(conn,target_ref)
            tid=int(trg['id']) if trg else None
        else:tid=None
        if tid in ids:
            idx=ids.index(tid)+(1 if position=='after' else 0)
            ids.insert(idx,int(src['id']))
        else:ids.append(int(src['id']))
        for i,nid in enumerate(ids):conn.execute("UPDATE nodes SET position=? WHERE id=?",(i,nid))
        _flat_write_recovery(conn)
        return before,ids,pid
    before,after,pid=_with_db(op)
    if record:record_action({'type':'flat_reorder','label':'Reorder item','parentID':pid,'beforeIDs':before,'afterIDs':after})
    return src_ref

def _flat_move(src_ref,dest_ref,target_ref=None,position='end',record=True):
    def op(conn):
        src=_flat_row(conn,src_ref);dest=_flat_row(conn,dest_ref)
        if not src or not dest or dest['kind'] not in {'root','folder'}:raise RuntimeError('Item or destination no longer exists.')
        old_pid=int(src['parent_id']);new_pid=int(dest['id']);old_pos=int(src['position'])
        if int(src['id'])==new_pid:raise RuntimeError('A folder cannot be moved into itself.')
        # prevent moving folder into descendant
        cur=dest
        while cur and cur['parent_id'] is not None:
            if int(cur['id'])==int(src['id']):raise RuntimeError('A folder cannot be moved into its descendant.')
            cur=conn.execute("SELECT * FROM nodes WHERE id=?",(int(cur['parent_id']),)).fetchone()
        new_pos=conn.execute("SELECT COALESCE(MAX(position),-1)+1 n FROM nodes WHERE parent_id=?",(new_pid,)).fetchone()['n']
        conn.execute("UPDATE nodes SET parent_id=?,position=?,modified_at=? WHERE id=?",(new_pid,int(new_pos),capture_timestamp(),int(src['id'])))
        _flat_reindex(conn,old_pid)
        if target_ref:
            trg=_flat_row(conn,target_ref)
            if trg and int(trg['parent_id'])==new_pid:
                ids=[int(r['id']) for r in _flat_children(conn,new_pid) if int(r['id'])!=int(src['id'])]
                idx=ids.index(int(trg['id']))+(1 if position=='after' else 0)
                ids.insert(idx,int(src['id']))
                for i,nid in enumerate(ids):conn.execute("UPDATE nodes SET position=? WHERE id=?",(i,nid))
        _flat_write_recovery(conn)
        return int(src['id']),old_pid,old_pos,new_pid,int(conn.execute("SELECT position FROM nodes WHERE id=?",(int(src['id']),)).fetchone()['position'])
    nid,oldpid,oldpos,newpid,newpos=_with_db(op)
    if record:record_action({'type':'flat_move','label':'Move item','nodeID':nid,'oldParentID':oldpid,'oldPosition':oldpos,'newParentID':newpid,'newPosition':newpos})
    return _with_db(lambda conn:_flat_ref_for_id(conn,nid))

def _flat_copy(src_ref,dest_ref,record=True):
    created_rows=[];created_names=[]
    def op(conn):
        src=_flat_row(conn,src_ref);dest=_flat_row(conn,dest_ref)
        if not src or not dest:raise RuntimeError('Item or destination no longer exists.')
        def clone(row,parent_id):
            if row['kind']=='folder':
                nid=_flat_insert_folder(conn,parent_id,row['display_name'],_flat_extra(row))
                created_rows.append(dict(conn.execute("SELECT * FROM nodes WHERE id=?",(nid,)).fetchone()))
                for ch in _flat_children(conn,int(row['id'])):clone(ch,nid)
                return nid
            oldp=_flat_payload_path_from_row(row)
            if not oldp.exists():raise RuntimeError(f'Archived file is missing: {oldp.name}')
            newp=flat_archive_dest(oldp.suffix,row['display_name'],row['id']);shutil.copy2(oldp,newp);created_names.append(newp.name)
            nid=_flat_insert_bookmark(conn,parent_id,newp.name,row['display_name'],row['source_url'],row['accessed_at'],_flat_extra(row))
            created_rows.append(dict(conn.execute("SELECT * FROM nodes WHERE id=?",(nid,)).fetchone()))
            return nid
        root_new=clone(src,int(dest['id']))
        _flat_write_recovery(conn)
        return root_new
    nid=_with_db(op)
    if record:record_action({'type':'flat_created','label':'Copy item','rows':created_rows,'flatPhysicalNames':created_names})
    return _with_db(lambda conn:_flat_ref_for_id(conn,nid))

def _flat_delete(ref,record=True):
    """Delete a virtual StashLibrary node safely.

    If a bookmark's physical payload has already been manually removed, treat
    it as a stale catalogue entry and remove the SQLite row anyway. A missing
    payload must never prevent the user from cleaning up the bookmark tree.

    A fresh user delete invalidates Redo immediately. This is important after
    Delete -> Undo -> Delete again: the previous redo action may still own an
    empty recovery directory/history snapshot for the same restored node.
    Clearing it before allocating the new delete action gives the second delete
    a completely fresh recovery state.
    """
    if record:
        _clear_redo()

    action={'type':'flat_delete','label':'Delete item'}

    def op(conn):
        row=_flat_row(conn,ref)
        if not row:
            # Already gone is effectively a successful delete from the UI's
            # point of view; this also makes stale/double delete requests safe.
            return None,None

        rows=_flat_snapshot_subtree(conn,int(row['id']))
        action['label']=f"Delete {row['display_name']}"
        parent_id=int(row['parent_id'])
        action['rows']=rows
        action['parentID']=parent_id
        return rows,parent_id

    rows,parent_id=_with_db(op)
    if not rows:
        return True

    existing_payload_rows=[]
    missing_payload_rows=[]
    for row in rows:
        if row['kind']!='bookmark':
            continue
        p=flat_archive_dir()/row['physical_name']
        if p.exists():
            existing_payload_rows.append((row,p))
        else:
            missing_payload_rows.append(row)

    # If the subtree contains no recoverable physical payloads at all, don't
    # create a pointless empty undo-store directory. Remove the stale catalogue
    # row directly. Undo cannot restore bytes that were already manually deleted.
    if not existing_payload_rows:
        def delete_stale_db(conn):
            root_id=int(rows[0]['id'])
            conn.execute("DELETE FROM nodes WHERE id=?",(root_id,))
            _flat_reindex(conn,parent_id)
            _flat_delete_unreferenced_file_rows(conn,rows)
            if conn.execute("SELECT 1 FROM nodes WHERE id=?",(root_id,)).fetchone():
                raise RuntimeError('SQLite did not remove the stale bookmark.')
            _flat_write_recovery(conn)

        _with_db(delete_stale_db)
        return True

    # Normal reversible delete for any payloads that still exist. Missing
    # descendants are tolerated; they simply cannot be physically restored.
    store=_action_store(action)
    names=[]

    try:
        for row,p in existing_payload_rows:
            target=store/row['physical_name']
            shutil.move(str(p),str(target))
            names.append(row['physical_name'])

        def delete_db(conn):
            root_id=int(rows[0]['id'])
            conn.execute("DELETE FROM nodes WHERE id=?",(root_id,))
            _flat_reindex(conn,parent_id)
            _flat_delete_unreferenced_file_rows(conn,rows)
            if conn.execute("SELECT 1 FROM nodes WHERE id=?",(root_id,)).fetchone():
                raise RuntimeError('SQLite did not remove the deleted bookmark.')
            _flat_write_recovery(conn)

        _with_db(delete_db)

        action['flatPhysicalNames']=names
        action['storedDir']=_encode_history_path(store)

        # If some descendants were already missing, keep the delete working but
        # don't advertise a misleading fully-restorable undo action.
        if record and not missing_payload_rows:
            record_action(action)
        elif missing_payload_rows:
            try:
                shutil.rmtree(store,ignore_errors=True)
            except Exception:
                pass

    except Exception:
        # Best-effort rollback of payloads that were successfully moved.
        for name in names:
            p=store/name
            if p.exists():
                try:
                    shutil.move(str(p),str(flat_archive_dir()/name))
                except Exception:
                    pass
        try:
            shutil.rmtree(store,ignore_errors=True)
        except Exception:
            pass
        raise

    return True


def _flat_set_positions(conn,parent_id,ids):
    for i,nid in enumerate(ids):
        conn.execute("UPDATE nodes SET parent_id=?,position=? WHERE id=?",(int(parent_id),i,int(nid)))

def _flat_undo_action(a,redo=False):
    typ=a.get('type')
    if typ=='flat_rename':
        name=a['newName'] if redo else a['oldName']
        _with_db(lambda conn:(conn.execute("UPDATE nodes SET display_name=? WHERE id=?",(name,int(a['nodeID']))),_flat_write_recovery(conn)))
        return
    if typ=='flat_move':
        pid=a['newParentID'] if redo else a['oldParentID'];pos=a['newPosition'] if redo else a['oldPosition']
        def op(conn):
            nid=int(a['nodeID'])
            oldrow=conn.execute("SELECT parent_id FROM nodes WHERE id=?",(nid,)).fetchone()
            oldpid=int(oldrow['parent_id']) if oldrow and oldrow['parent_id'] is not None else None
            conn.execute("UPDATE nodes SET parent_id=?,position=? WHERE id=?",(int(pid),int(pos),nid))
            if oldpid is not None:_flat_reindex(conn,oldpid)
            rows=[int(r['id']) for r in _flat_children(conn,int(pid)) if int(r['id'])!=nid]
            rows.insert(min(int(pos),len(rows)),nid);_flat_set_positions(conn,int(pid),rows)
            _flat_write_recovery(conn)
        _with_db(op);return
    if typ=='flat_reorder':
        ids=a['afterIDs'] if redo else a['beforeIDs']
        _with_db(lambda conn:(_flat_set_positions(conn,int(a['parentID']),ids),_flat_write_recovery(conn)));return
    if typ in {'flat_created','flat_delete'}:
        rows=[dict(x) for x in a.get('rows') or []]
        physical=list(a.get('flatPhysicalNames') or [])
        store=_resolve_history_path(a.get('storedDir')) if a.get('storedDir') else None
        if typ=='flat_created':
            should_exist=redo
        else:
            should_exist=not redo
        root_id=int(rows[0]['id']) if rows else None
        if should_exist:
            # Restore physical bytes first. Previously the SQLite row was restored
            # before its backing file, which could expose a bookmark whose payload
            # did not yet exist and could leave a stale DB/file pairing if the file
            # move failed.
            moved_back=[]
            try:
                if physical:
                    if not store or not store.exists():
                        raise RuntimeError('Undo data for the deleted file is missing.')
                    for name in physical:
                        source=store/name
                        target=flat_archive_dir()/name
                        if not source.exists():
                            raise RuntimeError(f'Undo data is missing the archived file: {name}')
                        if target.exists():
                            raise RuntimeError(f'Cannot restore because a file already exists at: {target.name}')
                        shutil.move(str(source),str(target))
                        moved_back.append((source,target))

                    # Do not re-index the bookmark until every expected payload is
                    # physically present at exactly the filename SQLite will store.
                    for name in physical:
                        target=flat_archive_dir()/name
                        if not target.is_file():
                            raise RuntimeError(f'Undo restored the bookmark but not its file: {name}')

                def restore(conn):
                    _flat_restore_snapshot(conn,rows)

                    # Self-heal legacy v0.9.x undo snapshots into schema v2.
                    _repair_restored_bookmark_file_links(conn,rows)

                    for restored in rows:
                        if restored.get('kind')!='bookmark':
                            continue

                        check=conn.execute(
                            "SELECT n.id,n.file_id,n.physical_name,f.storage_name "
                            "FROM nodes n LEFT JOIN files f ON f.id=n.file_id "
                            "WHERE n.id=?",
                            (int(restored['id']),)
                        ).fetchone()

                        if not check:
                            raise RuntimeError('Undo could not restore the bookmark catalogue row.')
                        if check['file_id'] is None or not check['storage_name']:
                            raise RuntimeError(
                                'Undo restored the bookmark but did not restore its file record.'
                            )
                        if str(check['physical_name']) != str(check['storage_name']):
                            raise RuntimeError(
                                'Undo restored inconsistent physical filename metadata.'
                            )

                        payload=flat_archive_dir()/str(check['storage_name'])
                        if not payload.is_file():
                            raise RuntimeError(
                                f'Undo catalogue/file mismatch: {check["storage_name"]}'
                            )

                    _flat_write_recovery(conn)

                _with_db(restore)

            except Exception:
                # If SQLite restoration fails, put any restored bytes back into
                # Undo storage so the action remains recoverable rather than
                # leaving a half-restored bookmark.
                if store:
                    store.mkdir(parents=True,exist_ok=True)
                    for source,target in reversed(moved_back):
                        try:
                            if target.exists() and not source.exists():
                                shutil.move(str(target),str(source))
                        except Exception:
                            pass
                raise
        else:
            if store is None:
                store=_action_store(a);a['storedDir']=_encode_history_path(store)
            store.mkdir(parents=True,exist_ok=True)
            for name in physical:
                p=flat_archive_dir()/name
                if p.exists():shutil.move(str(p),str(store/name))
            if root_id:
                def dele(conn):
                    row=conn.execute("SELECT parent_id FROM nodes WHERE id=?",(root_id,)).fetchone()
                    pid=int(row['parent_id']) if row and row['parent_id'] is not None else None
                    conn.execute("DELETE FROM nodes WHERE id=?",(root_id,))
                    if pid is not None:_flat_reindex(conn,pid)
                    _flat_delete_unreferenced_file_rows(conn,rows)
                    _flat_write_recovery(conn)
                _with_db(dele)
        return
    raise RuntimeError('This flat-archive action cannot be undone/redone.')

def _flat_create_snapshot_action_for_new(ref,label):
    def op(conn):
        row=_flat_row(conn,ref)
        rows=_flat_snapshot_subtree(conn,int(row['id']))
        names=[r['physical_name'] for r in rows if r['kind']=='bookmark']
        return rows,names
    rows,names=_with_db(op)
    return {'type':'flat_created','label':label,'rows':rows,'flatPhysicalNames':names}

def _flat_convert_existing():
    root=bookmarks_path()
    if not root:raise RuntimeError('Choose a bookmarks folder first.')
    root=Path(root)
    _clear_all_history()
    arc=flat_archive_dir()

    def op(conn):
        _db_ensure_imported(conn)
        rows=conn.execute("SELECT * FROM nodes WHERE kind!='root' ORDER BY id").fetchall()
        byid={int(r['id']):r for r in rows}

        def old_path(row):
            parts=[row['physical_name']]
            pid=row['parent_id']
            while pid and int(pid)!=_db_root_id(conn):
                pr=byid.get(int(pid)) or conn.execute("SELECT * FROM nodes WHERE id=?",(int(pid),)).fetchone()
                if not pr:break
                parts.append(pr['physical_name']);pid=pr['parent_id']
            return root.joinpath(*reversed(parts))

        owned_folder_paths=[]
        files_moved=0

        # Move only files represented by SQLite bookmark rows.
        for row in rows:
            if row['kind']!='bookmark':continue
            src=old_path(row)
            if not src.exists():
                existing=arc/row['physical_name']
                if existing.exists():continue
                raise RuntimeError(f'Cannot flatten because an archived file is missing: {row["display_name"]}')
            dest=flat_archive_dest(src.suffix,normalized_display_title(name or src.stem,src.suffix))
            shutil.move(str(src),str(dest))
            conn.execute("UPDATE nodes SET physical_name=? WHERE id=?",(dest.name,int(row['id'])))
            files_moved+=1

        # Record the old physical folders before turning them into logical nodes.
        for row in rows:
            if row['kind']=='folder':
                owned_folder_paths.append(old_path(row))
                conn.execute("UPDATE nodes SET physical_name=? WHERE id=?",(f'f{int(row["id"])}',int(row['id'])))

        _flat_write_recovery(conn)
        return files_moved,len(owned_folder_paths),owned_folder_paths

    files_count,folders_count,owned_folders=_with_db(op)

    # Remove old per-folder StashLibrary control files, then remove catalogue-owned
    # directories only when empty. If an unrelated/manual file exists inside one,
    # the directory is deliberately left in place rather than deleting user data.
    leftovers=[]
    for folder in sorted((Path(x) for x in owned_folders),key=lambda p:len(p.parts),reverse=True):
        if not folder.exists() or not folder.is_dir():continue
        for control in (metadata_file(folder),order_file(folder)):
            try:
                if control.exists():control.unlink()
            except Exception:pass
        try:
            folder.rmdir()
        except OSError:
            leftovers.append(str(folder))

    # Root-level legacy recovery mirrors are no longer authoritative in flat mode.
    # Keep them rather than deleting them; they may still be useful for manual recovery.
    c=read_config()
    c['storage_layout']='flat-sqlite-v2'
    c['flat_archive_converted_at']=capture_timestamp()
    write_config(c)

    return {
        'files':files_count,
        'folders':folders_count,
        'archive':str(arc),
        'leftoverPhysicalFolders':leftovers
    }


def _flat_rebuild_database_from_recovery():
    f=flat_recovery_file()
    if not f or not f.exists():raise RuntimeError('Flat catalogue recovery file was not found.')
    data=json.loads(f.read_text(encoding='utf-8'))
    rows=data.get('nodes') or []
    file_rows=data.get('files') or []
    db=database_file()
    with DB_LOCK:
        if db.exists():
            archived=db.with_name(f'stashlibrary-before-rebuild-{datetime.now().strftime("%Y%m%d-%H%M%S")}.sqlite3')
            shutil.copy2(db,archived)
        for suffix in ('','-wal','-shm'):
            p=Path(str(db)+suffix)
            if p.exists():p.unlink()
        conn=_db_connect()
        try:
            conn.execute("DELETE FROM nodes")
            conn.execute("DELETE FROM files")

            for fr in sorted(file_rows,key=lambda x:int(x.get('id',0))):
                conn.execute(
                    "INSERT INTO files(id,file_guid,storage_name,extension,size_bytes,sha256,created_at,modified_at) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (fr.get('id'),fr.get('file_guid') or _stable_guid('f_'),fr.get('storage_name',''),
                     fr.get('extension',''),fr.get('size_bytes',0),fr.get('sha256',''),
                     fr.get('created_at',''),fr.get('modified_at',''))
                )

            # Parent rows must exist before children, so reuse the snapshot restorer.
            _flat_restore_snapshot(conn,rows)

            conn.execute("INSERT OR REPLACE INTO catalogue_meta(key,value) VALUES('initial_import_complete',?)",(capture_timestamp(),))
            conn.execute("INSERT OR REPLACE INTO catalogue_meta(key,value) VALUES('schema_version',?)",(str(DATABASE_SCHEMA_VERSION),))
            conn.commit()
        finally:
            conn.close()
    return database_info()

FRIENDLY_HTTP_HOST='127.0.0.1'
FRIENDLY_HTTP_PORT=38671
FRIENDLY_HTTP_LOCK=threading.RLock()
FRIENDLY_HTTP_SERVER=None
FRIENDLY_HTTP_THREAD=None
FRIENDLY_ROUTES={}


def _safe_url_filename(storage):
    """Sanitize an already-existing physical filename for the readable URL."""
    name=str(storage or 'document').strip() or 'document'

    # The physical archive filename should already be Windows-safe. This is only
    # a defensive URL-display cleanup; it does not alter the physical file.
    name=re.sub(r'[<>:"/\\|?*]+',' - ',name)
    name=re.sub(r'\s+',' ',name).strip(' .') or 'document'

    # Preserve the real extension while keeping the visible URL reasonably short.
    if len(name)>180:
        p=Path(name)
        suffix=p.suffix
        stem=p.stem
        keep=max(1,180-len(suffix))
        name=stem[:keep].rstrip(' .-_')+suffix

    return name


def _cleanup_legacy_view_dir():
    """Remove v0.7.6/0.7.7 readable hard-link/copy aliases.

    The canonical flat archive files are untouched. A hard link is only another
    directory entry, and compatibility-copy aliases are redundant by design.
    """
    root=bookmarks_path()
    if not root:return
    if uses_separated_internal_data():return
    view=Path(root)/SYSTEM_DIR_NAME/'view'
    if not view.exists() or not view.is_dir():return
    try:
        for p in view.iterdir():
            try:
                if p.is_file() or p.is_symlink():p.unlink()
            except:pass
        try:view.rmdir()
        except:pass
    except:pass


class _StashLibraryFriendlyHandler(http.server.BaseHTTPRequestHandler):
    protocol_version='HTTP/1.1'

    def log_message(self,format,*args):
        return

    def _lookup(self):
        # Firefox requests the readable route URL-encoded, while route keys from
        # older/current StashLibrary builds may be stored encoded or decoded. Accept
        # both forms without changing the physical filename or serving logic.
        encoded_path=urllib.parse.urlsplit(self.path).path
        decoded_path=urllib.parse.unquote(encoded_path)
        with FRIENDLY_HTTP_LOCK:
            entry=FRIENDLY_ROUTES.get(encoded_path)
            if entry is None:
                entry=FRIENDLY_ROUTES.get(decoded_path)
            if entry is None:
                # Also tolerate a key that was registered from the decoded path
                # and then quoted by a previous build.
                requoted=urllib.parse.quote(decoded_path, safe='/')
                entry=FRIENDLY_ROUTES.get(requoted)
        if not entry:return None
        p=Path(entry['path'])
        if not p.exists() or not p.is_file():return None
        return p,entry

    def _serve(self,send_body=True):
        found=self._lookup()
        if not found:
            self.send_response(404);self.send_header('Content-Length','0');self.end_headers();return
        p,entry=found
        size=p.stat().st_size
        content_type=mimetypes.guess_type(str(p))[0] or 'application/octet-stream'
        start=0;end=size-1;status=200
        rng=self.headers.get('Range','')
        if rng.startswith('bytes='):
            try:
                spec=rng[6:].split(',',1)[0].strip();a,b=spec.split('-',1)
                if a:
                    start=int(a);end=int(b) if b else size-1
                elif b:
                    length=int(b);start=max(0,size-length);end=size-1
                start=max(0,min(start,size-1));end=max(start,min(end,size-1));status=206
            except: start=0;end=size-1;status=200
        length=max(0,end-start+1)
        self.send_response(status)
        self.send_header('Content-Type',content_type)
        self.send_header('Accept-Ranges','bytes')
        self.send_header('Content-Length',str(length))
        self.send_header('Cache-Control','no-store')
        if status==206:self.send_header('Content-Range',f'bytes {start}-{end}/{size}')
        self.end_headers()
        if not send_body:return
        with p.open('rb') as f:
            f.seek(start);remaining=length
            while remaining>0:
                chunk=f.read(min(1024*1024,remaining))
                if not chunk:break
                self.wfile.write(chunk);remaining-=len(chunk)

    def do_GET(self):self._serve(True)
    def do_HEAD(self):self._serve(False)


def _ensure_friendly_http_server():
    global FRIENDLY_HTTP_SERVER,FRIENDLY_HTTP_THREAD,FRIENDLY_HTTP_PORT
    with FRIENDLY_HTTP_LOCK:
        if FRIENDLY_HTTP_SERVER:return FRIENDLY_HTTP_PORT
        _cleanup_legacy_view_dir()
        try:
            srv=http.server.ThreadingHTTPServer((FRIENDLY_HTTP_HOST,FRIENDLY_HTTP_PORT),_StashLibraryFriendlyHandler)
        except OSError:
            srv=http.server.ThreadingHTTPServer((FRIENDLY_HTTP_HOST,0),_StashLibraryFriendlyHandler)
            FRIENDLY_HTTP_PORT=int(srv.server_address[1])
        srv.daemon_threads=True
        FRIENDLY_HTTP_SERVER=srv
        t=threading.Thread(target=srv.serve_forever,name='StashLibraryFriendlyHTTP',daemon=True)
        FRIENDLY_HTTP_THREAD=t;t.start()
        return FRIENDLY_HTTP_PORT


def friendly_open_url(path,display_name=''):
    """Expose a canonical payload through a readable localhost URL.

    Important separation:
      - StashLibrary display name: UI metadata only.
      - files.storage_name: authoritative physical filename.
      - /view/... path: derived only from the physical filename.
    """
    s=_normalize_virtual_ref(path)
    node_id=None

    if is_flat_layout() and s.startswith(VIRTUAL_ROOT):
        def op(conn):
            row=_flat_row(conn,s)
            if not row or row['kind']!='bookmark':
                raise RuntimeError('StashLibrary bookmark no longer exists in the SQLite catalogue.')

            storage=_db_storage_name_for_row(conn,row)
            if not storage:
                raise RuntimeError('Bookmark has no physical file relationship.')

            payload=flat_archive_dir()/storage

            # Compatibility fallback: if the authoritative file-row name is stale
            # but the old physical_name still exists, repair the mapping first.
            if not payload.is_file():
                legacy_name=str(row['physical_name'] or '')
                legacy=flat_archive_dir()/legacy_name if legacy_name else None
                if legacy and legacy.is_file():
                    fid=row['file_id']
                    if fid is None:
                        fid=_db_register_file(conn,legacy.name,row['created_at'])
                        conn.execute(
                            "UPDATE nodes SET file_id=?,physical_name=?,modified_at=? WHERE id=?",
                            (int(fid),legacy.name,capture_timestamp(),int(row['id']))
                        )
                    else:
                        _db_set_file_storage_name(conn,int(fid),legacy.name)
                    _flat_write_recovery(conn)
                    payload=legacy
                    storage=legacy.name

            if not payload.is_file():
                raise RuntimeError(
                    f'StashLibrary catalogue/file mismatch: the bookmark points to "{storage}", '
                    'but that file is not present in the archive.'
                )

            display_title=str(row['display_name'] or display_name or payload.stem)
            return {
                'payload':str(payload),
                'displayTitle':display_title,
                'nodeId':int(row['id']),
                'storageName':storage
            }

        resolved=_with_db(op)
        payload=Path(resolved['payload'])
        display_title=resolved['displayTitle']
        node_id=resolved['nodeId']
        storage_name=resolved['storageName']

    else:
        payload=Path(s)
        if not payload.exists() or not payload.is_file():
            raise RuntimeError('Bookmark file no longer exists.')
        display_title=str(display_name or payload.stem)
        storage_name=payload.name

    port=_ensure_friendly_http_server()

    # The visible Firefox URL is based on the real physical filename only.
    filename=_safe_url_filename(storage_name)
    route='/view/'+urllib.parse.quote(filename,safe='')

    decoded_route=urllib.parse.unquote(route)

    with FRIENDLY_HTTP_LOCK:
        entry={
            'path':str(payload),
            'title':display_title,
            'nodeID':node_id,
            'registeredAt':time.time()
        }
        # Register both canonical encoded and decoded representations. The
        # request handler also checks both, eliminating browser/path-decoding
        # differences as a source of 404s.
        FRIENDLY_ROUTES.pop(route,None)
        FRIENDLY_ROUTES.pop(decoded_route,None)
        FRIENDLY_ROUTES[route]=entry
        FRIENDLY_ROUTES[decoded_route]=entry

    return {
        'url':f'http://{FRIENDLY_HTTP_HOST}:{port}{route}',
        'title':display_title,
        'path':str(payload)
    }


def send_friendly_url_to_zotero(url,target=None):
    """Send a registered StashLibrary viewer URL using its catalogue metadata."""
    try:parsed=urllib.parse.urlsplit(str(url or ''))
    except Exception:return {'matched':False}
    if parsed.scheme.lower()!='http' or parsed.hostname!=FRIENDLY_HTTP_HOST:
        return {'matched':False}
    try:port=parsed.port or 80
    except ValueError:return {'matched':False}
    if port!=FRIENDLY_HTTP_PORT or not parsed.path.startswith('/view/'):
        return {'matched':False}
    encoded_path=parsed.path;decoded_path=urllib.parse.unquote(encoded_path)
    with FRIENDLY_HTTP_LOCK:
        entry=(FRIENDLY_ROUTES.get(encoded_path) or FRIENDLY_ROUTES.get(decoded_path)
               or FRIENDLY_ROUTES.get(urllib.parse.quote(decoded_path,safe='/')))
        entry=dict(entry) if entry else None
    if not entry:return {'matched':False}
    node_id=entry.get('nodeID')
    if node_id is not None and is_flat_layout():
        ref=_with_db(lambda conn:_flat_ref_for_id(conn,int(node_id)))
        if not ref:return {'matched':False}
    else:ref=str(entry.get('path') or '')
    return {'matched':True,**send_to_zotero(ref,target)}


def _force_explorer_window_foreground(folder_name=''):
    if os.name!='nt':return
    try:
        import ctypes
        from ctypes import wintypes
        user32=ctypes.windll.user32
        kernel32=ctypes.windll.kernel32
        SW_RESTORE=9
        target_name=str(folder_name or '').lower()
        candidates=[]
        EnumProc=ctypes.WINFUNCTYPE(wintypes.BOOL,wintypes.HWND,wintypes.LPARAM)

        def enum_cb(hwnd,lparam):
            if not user32.IsWindowVisible(hwnd):return True
            cls=ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(hwnd,cls,256)
            if cls.value not in ('CabinetWClass','ExploreWClass'):return True
            length=user32.GetWindowTextLengthW(hwnd)
            title=ctypes.create_unicode_buffer(length+1)
            user32.GetWindowTextW(hwnd,title,length+1)
            score=1
            if target_name and target_name in title.value.lower():score=10
            candidates.append((score,hwnd,title.value))
            return True
        cb=EnumProc(enum_cb)

        for _ in range(12):
            candidates.clear();user32.EnumWindows(cb,0)
            if candidates:
                candidates.sort(key=lambda x:x[0],reverse=True)
                hwnd=candidates[0][1]
                user32.ShowWindow(hwnd,SW_RESTORE)
                # Sending ALT briefly loosens Windows' foreground-lock rule for
                # a user-initiated shell action, then AttachThreadInput handles
                # existing/reused Explorer windows.
                VK_MENU=0x12;KEYEVENTF_KEYUP=0x0002
                user32.keybd_event(VK_MENU,0,0,0);user32.keybd_event(VK_MENU,0,KEYEVENTF_KEYUP,0)
                fg=user32.GetForegroundWindow()
                cur_tid=kernel32.GetCurrentThreadId()
                fg_tid=user32.GetWindowThreadProcessId(fg,None) if fg else 0
                target_tid=user32.GetWindowThreadProcessId(hwnd,None)
                if fg_tid:user32.AttachThreadInput(cur_tid,fg_tid,True)
                if target_tid and target_tid!=cur_tid:user32.AttachThreadInput(cur_tid,target_tid,True)
                user32.BringWindowToTop(hwnd);user32.SetForegroundWindow(hwnd);user32.SetFocus(hwnd)
                if target_tid and target_tid!=cur_tid:user32.AttachThreadInput(cur_tid,target_tid,False)
                if fg_tid:user32.AttachThreadInput(cur_tid,fg_tid,False)
                return
            time.sleep(.08)
    except Exception:
        pass

def _open_explorer_foreground(target,select=False):
    target=Path(target)
    if os.name!='nt':
        subprocess.Popen(['xdg-open',str(target if target.is_dir() else target.parent)])
        return
    folder=target.parent if select else target
    if select:
        subprocess.Popen(['explorer.exe',f'/select,{str(target)}'])
    else:
        # /separate strongly encourages a real foreground window instead of a
        # background reuse of an existing Explorer process.
        subprocess.Popen(['explorer.exe','/separate,',str(target)])

    # Do not make the native-message reply wait while Windows decides which
    # Explorer window to foreground. The old synchronous focus assist made
    # Open Files/Open Folder Location look stuck even though Explorer had
    # already been launched.
    def focus_later():
        time.sleep(.18)
        _force_explorer_window_foreground(folder.name)
    threading.Thread(target=focus_later,daemon=True).start()


def open_file_location(path):
    s=_normalize_virtual_ref(path)

    if is_flat_layout() and s.startswith(VIRTUAL_ROOT):
        def op(conn):
            row=_flat_row(conn,s)
            if not row:
                raise RuntimeError('StashLibrary item no longer exists.')
            if row['kind']=='bookmark':
                p=_flat_payload_path_from_row(row)
                if not p.exists():
                    raise RuntimeError(f'Archived file is missing: {p.name}')
                return 'file',p
            return 'folder',flat_archive_dir()
        kind,target=_with_db(op)
    else:
        p=Path(s)
        if not p.exists():
            raise RuntimeError('Item no longer exists on disk.')
        kind='folder' if p.is_dir() else 'file'
        target=p

    _open_explorer_foreground(target,select=(kind=='file'))
    return str(target)


def _flat_zotero_migrate(selected_collection_ids=None,selected_attachment_ids=None,destination_ref=None,direct_attachment_ids=None):
    inv=zotero_inventory()
    collections=inv.get('collections') or [];attachments=inv.get('attachments') or []
    coll_by_id={int(c['id']):c for c in collections if str(c.get('id','')).isdigit()}
    selected_collections={int(x) for x in (selected_collection_ids or []) if str(x).isdigit()}
    selected_atts={str(x) for x in (selected_attachment_ids or [])}
    direct={str(x) for x in (direct_attachment_ids or [])}
    # Attachments are opt-in. Selecting Zotero collections by itself must only
    # recreate the folder hierarchy; it must never copy every attachment in
    # the Zotero library. Filter to the explicitly selected attachment IDs,
    # or use an empty list when no files were selected.
    attachments=[a for a in attachments if str(a.get('id')) in selected_atts or str(a.get('key')) in selected_atts] if selected_atts else []
    dest_ref=destination_ref or VIRTUAL_ROOT

    def ensure_collection(conn,cid,parent_id,cache):
        cid=int(cid)
        if cid in cache:return cache[cid]
        c=coll_by_id.get(cid)
        if not c:return parent_id
        pid=c.get('parentID')
        actual_parent=ensure_collection(conn,int(pid),parent_id,cache) if pid and int(pid) in selected_collections else parent_id
        # match child by stored Zotero collection ID
        for row in _flat_children(conn,actual_parent):
            if row['kind']=='folder' and str(_flat_extra(row).get('zoteroCollectionID',''))==str(cid):
                cache[cid]=int(row['id']);return int(row['id'])
        nid=_flat_insert_folder(conn,actual_parent,c.get('name') or 'Untitled collection',{'zoteroCollectionID':str(cid)})
        cache[cid]=nid;return nid

    copied=skipped=missing=failed=folders_created=0;errors=[]
    manifest=load_zotero_manifest(bookmarks_path());entries=manifest.setdefault('entries',{})
    def op(conn):
        nonlocal copied,skipped,missing,failed,folders_created
        dest=_flat_row(conn,dest_ref)
        if not dest or dest['kind'] not in {'root','folder'}:raise RuntimeError('Chosen StashLibrary destination no longer exists.')
        base=int(dest['id']);cache={}
        before_folders=conn.execute("SELECT COUNT(*) n FROM nodes WHERE kind='folder'").fetchone()['n']
        for cid in selected_collections:ensure_collection(conn,cid,base,cache)
        after_folders=conn.execute("SELECT COUNT(*) n FROM nodes WHERE kind='folder'").fetchone()['n'];folders_created=int(after_folders-before_folders)

        for a in attachments:
            src=Path(a.get('path') or '')
            title=str(a.get('parentTitle') or a.get('title') or src.stem or 'Attachment')
            if not src.is_file():
                missing+=1;errors.append(f'Skipped: "{title}" — no local attachment file was available');continue
            ids={str(a.get('id')),str(a.get('key'))};is_direct=bool(ids&direct)
            collection_ids=[int(x) for x in (a.get('collectionIDs') or []) if str(x).isdigit()]
            parent_id=base
            if not is_direct:
                chosen=[cid for cid in collection_ids if cid in selected_collections]
                if chosen:parent_id=ensure_collection(conn,chosen[0],base,cache)

            identity=str(a.get('key') or a.get('id'))
            st=src.stat();rec=entries.get(identity) or {}
            old_node_id=rec.get('nodeID')
            oldrow=conn.execute("SELECT * FROM nodes WHERE id=?",(old_node_id,)).fetchone() if old_node_id else None
            if oldrow and rec.get('size')==st.st_size and rec.get('mtime_ns')==st.st_mtime_ns:
                conn.execute(
                    "UPDATE nodes SET parent_id=?,display_name=?,source_url=?,accessed_at=? WHERE id=?",
                    (parent_id,title,str(a.get('sourceURL') or a.get('sourceUrl') or ''),str(a.get('accessDate') or a.get('accessedAt') or ''),int(oldrow['id']))
                );skipped+=1;continue
            if oldrow:
                payload=flat_archive_dir()/oldrow['physical_name']
            else:
                payload=flat_archive_dest(src.suffix,title,a.get('id') or a.get('key'))
            shutil.copy2(src,payload)
            if oldrow:
                nid=int(oldrow['id'])
                conn.execute("UPDATE nodes SET parent_id=?,display_name=?,source_url=?,accessed_at=? WHERE id=?",
                             (parent_id,title,str(a.get('sourceURL') or a.get('sourceUrl') or ''),str(a.get('accessDate') or a.get('accessedAt') or ''),nid))
            else:
                nid=_flat_insert_bookmark(
                    conn,parent_id,payload.name,title,
                    str(a.get('sourceURL') or a.get('sourceUrl') or ''),
                    str(a.get('accessDate') or a.get('accessedAt') or ''),
                    {'originalFilename':src.name}
                )
                copied+=1
            entries[identity]={'nodeID':nid,'source':str(src),'size':st.st_size,'mtime_ns':st.st_mtime_ns,'zoteroKey':a.get('key'),'zoteroID':a.get('id')}
        _flat_write_recovery(conn)
    _with_db(op);save_zotero_manifest(bookmarks_path(),manifest)
    return {'copied':copied,'updated':0,'skipped':skipped,'missing':missing,'failed':failed,'foldersCreated':folders_created,'errors':errors,'attachments':len(attachments)}


def _database_tree():
    root=bookmarks_path()
    if not root:return None
    root=Path(root)

    if is_flat_layout():
        _ensure_readable_flat_filenames()
        def op(conn):
            _flat_reconcile_external_file_renames(conn)
            rows=conn.execute(
                "SELECT n.*,f.file_guid,f.storage_name FROM nodes n LEFT JOIN files f ON f.id=n.file_id WHERE n.kind!='root' ORDER BY n.parent_id,n.position,n.id"
            ).fetchall()
            children={}
            for row in rows:children.setdefault(int(row['parent_id']),[]).append(row)
            root_id=_db_root_id(conn)
            def build(row,ancestry):
                ids=ancestry+[int(row['id'])]
                ref=_virtual_ref_from_ids(ids)
                if row['kind']=='folder':
                    return {'type':'folder','name':str(row['display_name']),'path':ref,
                            'nodeGuid':str(row['node_guid'] or ''),
                            'children':[build(ch,ids) for ch in children.get(int(row['id']),[])]}
                storage=str(row['storage_name'] or row['physical_name'] or '')
                ext=Path(storage).suffix.lower()
                return {
                    'type':'bookmark','name':str(row['display_name']),'path':ref,'ext':ext,
                    'sourceUrl':str(row['source_url'] or ''),'accessedAt':str(row['accessed_at'] or ''),
                    'nodeGuid':str(row['node_guid'] or ''),'fileGuid':str(row['file_guid'] or ''),
                    'physicalName':storage
                }
            return {'type':'folder','name':'bookmarks','path':VIRTUAL_ROOT,
                    'children':[build(r,[]) for r in children.get(root_id,[])]}
        return _with_db(op)

    def op(conn):
        def sync_recursive(folder):
            folder=Path(folder);_db_sync_folder(conn,folder)
            for p in _raw_visible_entries(folder):
                if p.is_dir():sync_recursive(p)
        sync_recursive(root)
        rows=conn.execute("SELECT * FROM nodes WHERE kind!='root' ORDER BY parent_id,position,id").fetchall()
        children={}
        for row in rows:children.setdefault(int(row['parent_id']),[]).append(row)
        root_id=_db_root_id(conn)
        def build(row,parent_path):
            physical=str(row['physical_name']);path=Path(parent_path)/physical
            if row['kind']=='folder':
                return {'type':'folder','name':str(row['display_name'] or physical),'path':str(path),
                        'children':[build(ch,path) for ch in children.get(int(row['id']),[])]}
            return {'type':'bookmark','name':str(row['display_name'] or path.stem),'path':str(path),
                    'ext':path.suffix.lower(),'sourceUrl':str(row['source_url'] or ''),'accessedAt':str(row['accessed_at'] or '')}
        return {'type':'folder','name':'bookmarks','path':str(root),'children':[build(r,root) for r in children.get(root_id,[])]}
    return _with_db(op)

def tree_node(p):
    # Retained only for compatibility with older internal callers.
    p=Path(p)
    if p.is_dir():
        return {
            'type':'folder','name':get_display_name(p),'path':str(p),
            'children':[tree_node(x) for x in visible_entries(p)]
        }
    return {
        'type':'bookmark','name':get_display_name(p),'path':str(p),
        'ext':p.suffix.lower(),'sourceUrl':get_source_url(p)
    }

def get_tree():
    return _database_tree()

def snapshot_sig():
    b=bookmarks_path()
    if not b or not b.exists():return None
    try:
        if is_flat_layout():
            # Flat SQLite storage keeps the logical tree in SQLite and every
            # physical payload in one archive directory. The old watcher stat'ed
            # every archived HTML/PDF twice a second; with hundreds of captures
            # that created constant disk work and made Explorer/UI actions feel
            # sluggish. Logical StashLibrary edits already refresh the popup directly,
            # so the watcher only needs to notice external add/delete/rename
            # activity in the physical archive. Directory mtime changes cover it.
            arc=flat_archive_dir()
            if not arc:return None
            st=arc.stat()
            return ('flat',int(st.st_mtime_ns))

        # Legacy layouts: watch directory structure rather than stat'ing every
        # potentially large archived document. Renames/additions/deletions update
        # the containing directory's mtime.
        out=[]
        for root,dirs,files in os.walk(b):
            try:
                rp=Path(root)
                st=rp.stat()
                out.append((str(rp.relative_to(b)),int(st.st_mtime_ns),tuple(sorted(dirs)),tuple(sorted(files))))
            except Exception:
                pass
        return hash(tuple(out))
    except Exception:
        return None

def watcher():
    global last_sig
    while True:
        # UI background refreshes are already coalesced to a five-second window;
        # checking the filesystem more than once a second only wastes I/O.
        time.sleep(1.25); s=snapshot_sig()
        if last_sig is None:last_sig=s
        elif s!=last_sig:
            last_sig=s; send({'event':'changed'})
threading.Thread(target=watcher,daemon=True).start()

def unique_dest(folder,name,ext=''):
    p=folder/(safe_name(name)+ext);i=2
    while p.exists():p=folder/(safe_name(name)+f' ({i})'+ext);i+=1
    return p


_BASE36='0123456789abcdefghijklmnopqrstuvwxyz'

def _base36(n):
    n=int(n)
    if n<0:raise ValueError('base36 requires a non-negative integer')
    if n==0:return '0'
    out=''
    while n:
        n,r=divmod(n,36)
        out=_BASE36[r]+out
    return out

def _history_reserved_physical_names(parent):
    """Names in this directory that must not be reused while Undo/Redo may
    still need them. This keeps short physical IDs compatible with history.
    """
    parent=Path(parent)
    reserved=set()
    for action in list(undo_stack)+list(redo_stack):
        for key in ('path','old','new'):
            value=action.get(key)
            if not value:continue
            try:
                p=_resolve_history_path(value)
                if p.parent.resolve(strict=False)==parent.resolve(strict=False):
                    reserved.add(p.name.lower())
            except Exception:
                pass
    return reserved

def _physical_id_taken(parent,ident):
    parent=Path(parent)
    ident=str(ident).lower()
    reserved=_history_reserved_physical_names(parent)

    for p in visible_entries(parent):
        stem=(p.name if p.is_dir() else p.stem).lower()
        if stem==ident:
            return True

    # Reserve the stem regardless of extension, so a directory never contains
    # both 3.pdf and 3.html or a folder named 3.
    for name in reserved:
        rp=Path(name)
        stem=(name if not rp.suffix else rp.stem).lower()
        if stem==ident:
            return True

    return False

def short_id_dest(parent,ext=''):
    """Allocate the lowest reusable short physical ID in a directory.

    Sequence:
      1..9, a..z, 10, 11, ...

    0 is intentionally skipped because 1 is friendlier for manual inspection.
    The same stem is never reused by another sibling, even with a different
    extension.
    """
    parent=Path(parent)
    ext=str(ext or '')
    if ext and not ext.startswith('.'):ext='.'+ext

    n=1
    while True:
        ident=_base36(n)
        if not _physical_id_taken(parent,ident):
            return parent/(ident+ext)
        n+=1

def _clear_all_history():
    global undo_stack,redo_stack
    for action in list(undo_stack)+list(redo_stack):
        _drop_action_storage(action)
    undo_stack=[]
    redo_stack=[]
    base=undo_dir()
    if base and base.exists():
        for p in list(base.iterdir()):
            try:
                if p.is_dir():shutil.rmtree(p,ignore_errors=True)
                else:p.unlink()
            except Exception:pass
    _save_history()


def _parse_accessed_datetime(value=None):
    """Best-effort parser for StashLibrary ISO timestamps and Zotero SQL/ISO dates."""
    if isinstance(value,datetime):
        return value.astimezone() if value.tzinfo else value
    s=str(value or '').strip()
    if not s:
        return datetime.now().astimezone()
    candidates=[s, s.replace('Z','+00:00')]
    for candidate in candidates:
        try:
            dt=datetime.fromisoformat(candidate)
            return dt.astimezone() if dt.tzinfo else dt
        except Exception:
            pass
    for fmt in ('%Y-%m-%d %H:%M:%S','%Y-%m-%d %H:%M','%Y-%m-%d'):
        try:return datetime.strptime(s,fmt)
        except Exception:pass
    return datetime.now().astimezone()

def _strip_existing_archive_stamp(stem):
    """Avoid stacking timestamps if an incoming file already has a StashLibrary/SingleFile-style suffix."""
    s=str(stem or '')
    patterns=[
        r'\s*\(\d{4}-\d{2}-\d{2}[ _]\d{2}[-_:]\d{2}[-_:]\d{2}(?:[-_]\d{3})?\)\s*$',
        r'[_ ]\d{4}[_-]\d{2}[_-]\d{2}[_-]\d{2}[_-]\d{2}[_-]\d{2}(?:[_-]\d{3})?\s*$'
    ]
    for pat in patterns:s=re.sub(pat,'',s)
    return s.strip()

def archival_dest(folder,name,ext='',accessed_at=None):
    """Allocate StashLibrary's short internal physical filename.

    The readable title and Accessed timestamp live in metadata instead of in
    the Windows filename. The original extension is kept so file type remains
    obvious and external programs can still recognise PDFs/HTML/etc.
    """
    folder=Path(folder)
    ext=str(ext or '')
    if not ext:
        try:ext=Path(str(name or '')).suffix
        except Exception:ext=''
    return short_id_dest(folder,ext)



def stop_process_tree(proc):
    if proc.poll() is not None:return
    try:
        if os.name=='nt': subprocess.run(['taskkill','/PID',str(proc.pid),'/T','/F'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=8)
        else:
            proc.terminate()
            try:proc.wait(timeout=5)
            except subprocess.TimeoutExpired:proc.kill()
    except Exception:
        try:proc.kill()
        except Exception:pass

def _strip_folder_timestamp(name):
    # Backwards compatibility for display fallback on older timestamped builds.
    s=str(name or '').strip()
    return re.sub(
        r'\s*\(\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}(?:-\d{3})?(?:-\d{3})?(?:-\d{3})?\)\s*$',
        '',
        s
    ).strip()

def timestamped_folder_dest(parent,name,created_at=None,created_ns=None):
    """Compatibility wrapper retained for older call sites.

    v0.5+ uses short internal folder IDs instead of timestamps.
    """
    p=short_id_dest(parent,'')
    ns=int(created_ns) if created_ns is not None else time.time_ns()
    return p,ns


def add_order(folder,name,before=None,after=None):
    current=[p.name for p in visible_entries(folder) if p.name!=name]
    if before and before in current: current.insert(current.index(before),name)
    elif after and after in current: current.insert(current.index(after)+1,name)
    else: current.append(name)
    save_order(folder,current)

def reorder_item(src,parent,target=None,position='end',record=True):
    src=Path(src); parent=Path(parent)
    before_order=load_order(parent) or [p.name for p in visible_entries(parent)]
    if src.parent.resolve()!=parent.resolve(): raise RuntimeError('Reorder source is not in the selected folder.')
    names=[p.name for p in visible_entries(parent)]
    if src.name not in names: raise RuntimeError('Item no longer exists.')
    names.remove(src.name)
    if position=='end' or not target:names.append(src.name)
    else:
        t=Path(target).name
        if t not in names: raise RuntimeError('Drop target no longer exists.')
        idx=names.index(t)+(1 if position=='after' else 0);names.insert(idx,src.name)
    save_order(parent,names)
    if record:record_action({'type':'reorder','label':f'Reorder {get_display_name(src)}','parent':str(parent),'before':before_order,'after':list(names)})
    return src

def get_title_from_url(url):
    path=urllib.parse.urlparse(url).path
    return Path(path).stem or urllib.parse.urlparse(url).hostname or 'Bookmark'

def copy_file_with_progress(src,dest,operation='Copying'):
    total=src.stat().st_size; done=0; started=time.monotonic();progress(operation,'starting',src.name,0,started)
    with open(src,'rb') as rf, open(dest,'wb') as wf:
        while True:
            chunk=rf.read(1024*1024)
            if not chunk:break
            wf.write(chunk);done+=len(chunk);progress(operation,'copying file',src.name,(done/total*100 if total else 100),started)
    shutil.copystat(src,dest);progress(operation,'finalising',dest.name,100,started)

def save_url(parent,url,name):
    parent.mkdir(parents=True,exist_ok=True); parsed=urllib.parse.urlparse(url); started=time.monotonic();progress('Saving bookmark','checking source',url,started=started)
    if parsed.scheme=='file': src=Path(urllib.request.url2pathname(parsed.path))
    elif parsed.scheme=='': src=Path(url)
    else: src=None
    if src is not None and src.exists():
        accessed=capture_timestamp()
        dest=archival_dest(parent,name or src.stem,src.suffix,accessed)
        copy_file_with_progress(src,dest,'Saving local file');add_order(parent,dest.name)
        set_item_metadata(parent,dest.name,title=normalized_display_title(name or src.stem,src.suffix),source_url=url,accessed_at=accessed)
        progress('Saving bookmark','saved local file',dest.name,100,started);return dest
    is_pdf=parsed.path.lower().endswith('.pdf')
    if is_pdf:
        accessed=capture_timestamp()
        dest=archival_dest(parent,name or get_title_from_url(url),'.pdf',accessed);progress('Saving PDF','connecting',urllib.parse.urlparse(url).hostname or url,0,started)
        req=urllib.request.Request(url,headers={'User-Agent':'Mozilla/5.0'})
        with urllib.request.urlopen(req,timeout=60) as resp, open(dest,'wb') as f:
            total=int(resp.headers.get('Content-Length') or 0);done=0;progress('Saving PDF','downloading',dest.name,0 if total else None,started)
            while True:
                chunk=resp.read(1024*1024)
                if not chunk:break
                f.write(chunk);done+=len(chunk);progress('Saving PDF','downloading',dest.name,(done/total*100 if total else None),started)
        add_order(parent,dest.name);set_item_metadata(parent,dest.name,title=normalized_display_title(name or get_title_from_url(url),'.pdf'),source_url=url,accessed_at=accessed);progress('Saving PDF','finished',dest.name,100,started);return dest
    raise RuntimeError('Normal webpages are saved through the SingleFile Firefox extension, not the native helper.')

def save_pdf_data(parent,name,pdf_base64,display_title='',source_url=''):
    parent=Path(parent);parent.mkdir(parents=True,exist_ok=True);started=time.monotonic()
    try:data=base64.b64decode(pdf_base64,validate=True)
    except Exception as e:raise RuntimeError('Firefox supplied unreadable PDF data.') from e
    if not data:raise RuntimeError('The hosted PDF download was empty.')
    probe=data[:1024]
    if b'%PDF-' not in probe:raise RuntimeError('The hosted document did not contain a valid PDF signature.')
    base=Path(name or 'document.pdf').name
    stem=Path(base).stem or 'document';accessed=capture_timestamp();dest=archival_dest(parent,stem,'.pdf',accessed)
    progress('Saving hosted PDF','writing PDF to bookmark folder',dest.name,90,started)
    with open(dest,'wb') as f:f.write(data)
    add_order(parent,dest.name);set_item_metadata(parent,dest.name,title=normalized_display_title(display_title or stem,'.pdf'),source_url=source_url,accessed_at=accessed);progress('Saving hosted PDF','finished',dest.name,100,started);return dest

def import_download(src,dest,display_title='',source_url=''):
    src=Path(src); dest=Path(dest); dest.mkdir(parents=True,exist_ok=True); started=time.monotonic()
    if not src.exists(): raise RuntimeError(f'Finished SingleFile download was not found: {src}')
    accessed=capture_timestamp()
    shown=normalized_display_title(display_title or src.stem,src.suffix)
    target=archival_dest(dest,display_title or src.stem,src.suffix,accessed)
    progress('Complete save','moving archive from Firefox downloads',src.name,95,started)
    if src.resolve()!=target.resolve():
        try:shutil.move(str(src),str(target))
        except Exception:
            shutil.copy2(src,target)
            try:src.unlink()
            except Exception:pass
    add_order(dest,target.name)
    set_item_metadata(dest,target.name,title=shown,source_url=source_url,accessed_at=accessed)
    progress('Complete save','archive stored in bookmark folder',target.name,100,started);return target

def rename_bookmark(path,new_name,record=True):
    src=Path(path)
    if not src.exists():raise RuntimeError('Item no longer exists.')

    parent=src.parent
    is_folder=src.is_dir()
    suffix='' if is_folder else src.suffix
    before_order=load_order(parent) or [p.name for p in visible_entries(parent)]

    old_meta=get_item_metadata(src)
    old_display=get_display_name(src)
    display=(str(new_name or '').strip() if is_folder else normalized_display_title(new_name,suffix)) or old_display

    # In the short-ID storage layout, a rename is purely metadata. This keeps
    # paths stable and makes duplicate display names harmless.
    new_meta=dict(old_meta)
    new_meta['title']=display
    set_item_metadata(
        parent,
        src.name,
        title=display,
        source_url=new_meta.get('sourceUrl'),
        accessed_at=new_meta.get('accessedAt'),
        replace=True,
        extra={k:v for k,v in new_meta.items() if k not in {'title','sourceUrl','accessedAt'}}
    )

    if record:
        record_action({
            'type':'rename',
            'label':f'Rename {old_display}',
            'old':str(src),
            'new':str(src),
            'display_old':old_display,
            'display_new':display,
            'meta_old':old_meta,
            'meta_new':new_meta,
            'order_before':before_order,
            'order_after':before_order
        })

    progress('Renaming','display name updated',display,100)
    return src


def move_or_copy(src,dest,do_copy=False,target=None,position='end',record=True):
    src=Path(src);dest=Path(dest);dest.mkdir(parents=True,exist_ok=True)
    old=src.parent;started=time.monotonic();op='Copying' if do_copy else 'Moving'
    progress(op,'preparing',src.name,started=started)

    if not do_copy and old.resolve()==dest.resolve():
        return reorder_item(src,dest,target,position,record=record)

    item_meta=get_item_metadata(src)
    display=get_display_name(src)
    src_before=load_order(old) or [p.name for p in visible_entries(old)]
    dest_before=load_order(dest) or [p.name for p in visible_entries(dest)]

    ext='' if src.is_dir() else src.suffix
    target_path=short_id_dest(dest,ext)

    progress(op,'transferring',f'{display} → {dest.name}',started=started)

    if do_copy:
        if src.is_dir():shutil.copytree(src,target_path)
        else:copy_file_with_progress(src,target_path,'Copying file')
    else:
        shutil.move(str(src),str(target_path))
        moved_in_db=False
        try:moved_in_db=_db_move_node(src,target_path)
        except Exception:moved_in_db=False
        if not moved_in_db:
            remove_display_name(old,src.name)

    set_item_metadata(
        dest,
        target_path.name,
        title=display,
        source_url=item_meta.get('sourceUrl'),
        accessed_at=item_meta.get('accessedAt'),
        extra={k:v for k,v in item_meta.items() if k not in {'title','sourceUrl','accessedAt'}}
    )

    save_order(old,[p.name for p in visible_entries(old)])
    before=Path(target).name if target and position=='before' else None
    after=Path(target).name if target and position=='after' else None
    add_order(dest,target_path.name,before=before,after=after)

    src_after=load_order(old);dest_after=load_order(dest)

    if record:
        if do_copy:
            record_action({
                'type':'created','label':f'Copy {display}','path':str(target_path),
                'display':display,'meta':get_item_metadata(target_path),
                'order_before':dest_before,'order_after':dest_after
            })
        else:
            record_action({
                'type':'move','label':f'Move {display}','old':str(src),'new':str(target_path),
                'display':display,'meta':get_item_metadata(target_path),
                'src_order_before':src_before,'src_order_after':src_after,
                'dest_parent':str(dest),'dest_order_before':dest_before,'dest_order_after':dest_after
            })

    progress(op,'finished',target_path.name,100,started)
    return target_path


def _library_id_from_connection(conn):
    try:
        row=conn.execute("SELECT value FROM catalogue_meta WHERE key='library_id' LIMIT 1").fetchone()
        return str(row[0] if row else '').strip()
    except Exception:
        return ''


def current_library_id():
    try:
        with DB_LOCK:
            conn=_db_connect()
            try:return _library_id_from_connection(conn)
            finally:conn.close()
    except Exception:return ''


def _library_inventory_from_root(root):
    """Validate a self-contained StashLibrary folder and return its cloud-backup payload."""
    root=Path(root)
    data=root/UNIFIED_DATA_DIR_NAME
    db=data/DATABASE_NAME
    if not root.is_dir():raise RuntimeError('The selected StashLibrary folder does not exist.')
    if not db.is_file():raise RuntimeError(f'The selected folder is not a self-contained StashLibrary folder ({UNIFIED_DATA_DIR_NAME}\\{DATABASE_NAME} is missing).')
    try:
        with sqlite3.connect(f'file:{db.as_posix()}?mode=ro&immutable=1',uri=True) as conn:
            row=conn.execute('PRAGMA quick_check').fetchone()
            if not row or str(row[0]).lower()!='ok':raise RuntimeError('The selected StashLibrary catalogue failed its SQLite integrity check.')
            names=[str(r[0]) for r in conn.execute('SELECT storage_name FROM files').fetchall() if str(r[0] or '')]
            library_id=_library_id_from_connection(conn)
    except sqlite3.Error as e:raise RuntimeError(f'The selected StashLibrary catalogue could not be read: {e}')
    files={}
    missing=[]
    for name in names:
        if Path(name).name!=name:
            raise RuntimeError('The selected StashLibrary catalogue contains an unsafe archive filename.')
        q=root/name
        if not q.is_file():missing.append(name);continue
        st=q.stat();files[name]={'path':q,'size':int(st.st_size),'mtimeNs':int(st.st_mtime_ns)}
    if missing:
        preview=', '.join(missing[:3]);more=f' (+{len(missing)-3} more)' if len(missing)>3 else ''
        raise RuntimeError(f'The selected StashLibrary folder is incomplete: {len(missing)} archived file(s) are missing: {preview}{more}')
    return {'root':root,'db':db,'files':files,'libraryId':library_id}


# ----------------------------- WebDAV cloud backup -----------------------------

def _webdav_provider_label(provider):
    return {
        'koofr':'Koofr',
        'pcloud-eu':'pCloud (Europe)',
        'pcloud-us':'pCloud (US)',
        'nextcloud':'Nextcloud',
        'owncloud':'ownCloud',
        'other':'Other WebDAV',
    }.get(str(provider or '').strip().lower(),'WebDAV')


def _webdav_default_url(provider):
    return {
        'koofr':'https://app.koofr.net/dav/Koofr',
        'pcloud-eu':'https://ewebdav.pcloud.com',
        'pcloud-us':'https://webdav.pcloud.com',
    }.get(str(provider or '').strip().lower(),'')


def _webdav_normalize_url(value):
    raw=str(value or '').strip().rstrip('/')
    if not raw:raise RuntimeError('Enter the WebDAV server address.')
    parsed=urllib.parse.urlsplit(raw)
    if parsed.scheme not in {'https','http'} or not parsed.netloc:
        raise RuntimeError('Enter a complete WebDAV address beginning with https://.')
    insecure_ok=(parsed.hostname in {'127.0.0.1','localhost'} and os.environ.get('STASHLIBRARY_ALLOW_INSECURE_WEBDAV')=='1')
    if parsed.scheme!='https' and not insecure_ok:
        raise RuntimeError('StashLibrary requires an HTTPS WebDAV connection so your login is protected.')
    return urllib.parse.urlunsplit((parsed.scheme,parsed.netloc,parsed.path.rstrip('/'),parsed.query,''))


def _protect_webdav_secret(secret):
    data=str(secret or '').encode('utf-8')
    if not data:return ''
    if sys.platform!='win32':
        # Source/test fallback only. The distributed helper runs on Windows and
        # uses DPAPI below so the password is not stored as readable text.
        return 'test:'+base64.b64encode(data).decode('ascii')
    import ctypes
    from ctypes import wintypes
    class DATA_BLOB(ctypes.Structure):
        _fields_=[('cbData',wintypes.DWORD),('pbData',ctypes.POINTER(ctypes.c_ubyte))]
    buf=(ctypes.c_ubyte*len(data)).from_buffer_copy(data)
    in_blob=DATA_BLOB(len(data),ctypes.cast(buf,ctypes.POINTER(ctypes.c_ubyte)))
    out_blob=DATA_BLOB()
    crypt32=ctypes.WinDLL('crypt32',use_last_error=True);kernel32=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel32.LocalFree.argtypes=[ctypes.c_void_p];kernel32.LocalFree.restype=ctypes.c_void_p
    crypt32.CryptProtectData.argtypes=[ctypes.POINTER(DATA_BLOB),wintypes.LPCWSTR,ctypes.POINTER(DATA_BLOB),ctypes.c_void_p,ctypes.c_void_p,wintypes.DWORD,ctypes.POINTER(DATA_BLOB)]
    crypt32.CryptProtectData.restype=wintypes.BOOL
    if not crypt32.CryptProtectData(ctypes.byref(in_blob),'StashLibrary WebDAV backup',None,None,None,0x1,ctypes.byref(out_blob)):
        raise RuntimeError('Windows could not securely save the WebDAV password.')
    try:protected=ctypes.string_at(out_blob.pbData,out_blob.cbData)
    finally:
        if out_blob.pbData:kernel32.LocalFree(out_blob.pbData)
    return 'dpapi:'+base64.b64encode(protected).decode('ascii')


def _unprotect_webdav_secret(value):
    text=str(value or '')
    if not text:return ''
    if text.startswith('test:'):
        try:return base64.b64decode(text[5:]).decode('utf-8')
        except Exception:return ''
    if not text.startswith('dpapi:') or sys.platform!='win32':return ''
    import ctypes
    from ctypes import wintypes
    class DATA_BLOB(ctypes.Structure):
        _fields_=[('cbData',wintypes.DWORD),('pbData',ctypes.POINTER(ctypes.c_ubyte))]
    try:data=base64.b64decode(text[6:])
    except Exception:return ''
    buf=(ctypes.c_ubyte*len(data)).from_buffer_copy(data)
    in_blob=DATA_BLOB(len(data),ctypes.cast(buf,ctypes.POINTER(ctypes.c_ubyte)))
    out_blob=DATA_BLOB()
    crypt32=ctypes.WinDLL('crypt32',use_last_error=True);kernel32=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel32.LocalFree.argtypes=[ctypes.c_void_p];kernel32.LocalFree.restype=ctypes.c_void_p
    crypt32.CryptUnprotectData.argtypes=[ctypes.POINTER(DATA_BLOB),ctypes.POINTER(wintypes.LPWSTR),ctypes.POINTER(DATA_BLOB),ctypes.c_void_p,ctypes.c_void_p,wintypes.DWORD,ctypes.POINTER(DATA_BLOB)]
    crypt32.CryptUnprotectData.restype=wintypes.BOOL
    if not crypt32.CryptUnprotectData(ctypes.byref(in_blob),None,None,None,None,0x1,ctypes.byref(out_blob)):
        return ''
    try:return ctypes.string_at(out_blob.pbData,out_blob.cbData).decode('utf-8')
    except Exception:return ''
    finally:
        if out_blob.pbData:kernel32.LocalFree(out_blob.pbData)


def _webdav_config(require=False):
    c=read_config();provider=str(c.get('webdav_provider') or '').strip().lower()
    url=str(c.get('webdav_url') or '').strip();username=str(c.get('webdav_username') or '').strip()
    password=_unprotect_webdav_secret(c.get('webdav_secret') or '')
    enabled=bool(c.get('webdav_backup_enabled'))
    if require and (not enabled or not provider or not url or not username or not password):
        raise RuntimeError('Cloud Backup is not connected. Open Settings and connect a WebDAV provider first.')
    return {'enabled':enabled,'provider':provider,'url':url,'username':username,'password':password}


def _webdav_auth_header(username,password):
    token=base64.b64encode(f'{username}:{password}'.encode('utf-8')).decode('ascii')
    return 'Basic '+token


def _webdav_url(base,*parts):
    url=str(base or '').rstrip('/')
    for part in parts:
        for piece in str(part).replace('\\','/').split('/'):
            if piece:url+='/'+urllib.parse.quote(piece,safe='')
    return url


def _webdav_request(method,url,username,password,data=None,headers=None,allowed=None,timeout=45):
    hdr={'Authorization':_webdav_auth_header(username,password),'User-Agent':f'StashLibrary/{STASHLIBRARY_VERSION}'}
    hdr.update(headers or {})
    req=urllib.request.Request(url,data=data,headers=hdr,method=method)
    try:
        with urllib.request.urlopen(req,timeout=timeout) as resp:
            body=resp.read();code=int(getattr(resp,'status',200) or 200)
            if allowed and code not in allowed:raise RuntimeError(f'WebDAV returned HTTP {code}.')
            return code,body,dict(resp.headers)
    except urllib.error.HTTPError as e:
        if allowed and int(e.code) in allowed:
            try:body=e.read()
            except Exception:body=b''
            return int(e.code),body,dict(e.headers or {})
        detail=''
        try:detail=e.read(512).decode('utf-8','replace').strip()
        except Exception:pass
        suffix=f' — {detail}' if detail else ''
        raise RuntimeError(f'WebDAV returned HTTP {e.code}{suffix}')
    except urllib.error.URLError as e:
        raise RuntimeError(f'Could not reach the WebDAV server: {getattr(e,"reason",e)}')


def _webdav_stream_put(path,url,username,password,progress_cb=None,max_attempts=3):
    """Upload one file with retries and optional byte-level progress reporting."""
    path=Path(path);parts=urllib.parse.urlsplit(url);size=path.stat().st_size
    last_error=None
    for attempt in range(1,max(1,int(max_attempts))+1):
        conn_cls=http.client.HTTPSConnection if parts.scheme=='https' else http.client.HTTPConnection
        conn=conn_cls(parts.hostname,parts.port,timeout=75)
        target=urllib.parse.urlunsplit(('', '',parts.path or '/',parts.query,''))
        sent=0
        try:
            conn.putrequest('PUT',target)
            conn.putheader('Authorization',_webdav_auth_header(username,password))
            conn.putheader('User-Agent',f'StashLibrary/{STASHLIBRARY_VERSION}')
            conn.putheader('Content-Type','application/octet-stream')
            conn.putheader('Content-Length',str(size))
            conn.endheaders()
            with path.open('rb') as f:
                while True:
                    chunk=f.read(1024*1024)
                    if not chunk:break
                    conn.send(chunk);sent+=len(chunk)
                    if progress_cb:
                        try:progress_cb(sent,size,attempt)
                        except Exception:pass
            resp=conn.getresponse();body=resp.read(512)
            if resp.status not in {200,201,204}:
                detail=body.decode('utf-8','replace').strip()
                raise RuntimeError(f'WebDAV upload returned HTTP {resp.status}'+(f' — {detail}' if detail else ''))
            if progress_cb:
                try:progress_cb(size,size,attempt)
                except Exception:pass
            return
        except (TimeoutError,ConnectionError,OSError,http.client.HTTPException,RuntimeError) as e:
            last_error=e
            if attempt>=max_attempts:break
            time.sleep(min(1.0*attempt,3.0))
        finally:
            try:conn.close()
            except Exception:pass
    raise RuntimeError(f'WebDAV upload failed after {max_attempts} attempts: {last_error}')


def _webdav_remote_size(url,username,password):
    """Return the remote Content-Length when the WebDAV server supports HEAD."""
    try:
        code,_,headers=_webdav_request('HEAD',url,username,password,allowed={200,204,404,405,501},timeout=30)
        if code in {404,405,501}:return None
        value=headers.get('Content-Length') or headers.get('content-length')
        return int(value) if value is not None and str(value).isdigit() else None
    except Exception:
        return None


def _webdav_remote_sha256(url,username,password):
    """Stream a remote WebDAV object once and return (sha256, size).

    This is intentionally a fallback for older/incomplete StashLibrary backup metadata.
    Normal repeat backups use the SHA-256 values already stored in
    backup-state.json and therefore do not download unchanged archive payloads.
    """
    hdr={'Authorization':_webdav_auth_header(username,password),'User-Agent':f'StashLibrary/{STASHLIBRARY_VERSION}'}
    req=urllib.request.Request(url,headers=hdr,method='GET')
    h=hashlib.sha256();size=0
    try:
        with urllib.request.urlopen(req,timeout=120) as resp:
            while True:
                chunk=resp.read(1024*1024)
                if not chunk:break
                h.update(chunk);size+=len(chunk)
        return h.hexdigest(),size
    except urllib.error.HTTPError as e:
        if e.code==404:return None,None
        raise RuntimeError(f'Cloud backup comparison returned HTTP {e.code}.')
    except urllib.error.URLError as e:
        raise RuntimeError(f'Could not reach the WebDAV server while comparing the existing backup: {getattr(e,"reason",e)}')


def _webdav_list_remote_files(files_url,username,password):
    """Return the archive files that physically exist in the WebDAV backup.

    backup-state.json remains StashLibrary's fast content-index, but the remote folder
    is also enumerated so stale metadata cannot make StashLibrary needlessly PUT an
    already-present file or overlook a remote file that should be deleted.
    """
    body=(
        '<?xml version="1.0" encoding="utf-8"?>'
        '<d:propfind xmlns:d="DAV:"><d:prop>'
        '<d:resourcetype/><d:getcontentlength/><d:getetag/><d:getlastmodified/>'
        '</d:prop></d:propfind>'
    ).encode('utf-8')
    code,data,_=_webdav_request(
        'PROPFIND',files_url,username,password,data=body,
        headers={'Depth':'1','Content-Type':'application/xml; charset=utf-8'},
        allowed={207,404},timeout=45
    )
    if code==404:return {}
    try:root=ET.fromstring(data)
    except Exception:return {}
    target_path=urllib.parse.unquote(urllib.parse.urlsplit(files_url).path).rstrip('/')
    result={}
    for response in root.findall('.//{DAV:}response'):
        href_node=response.find('{DAV:}href')
        if href_node is None or not str(href_node.text or '').strip():continue
        href_path=urllib.parse.unquote(urllib.parse.urlsplit(str(href_node.text)).path).rstrip('/')
        if href_path==target_path:continue
        name=Path(href_path).name
        if not name or Path(name).name!=name:continue
        prop=None
        for propstat in response.findall('{DAV:}propstat'):
            status=str((propstat.findtext('{DAV:}status') or ''))
            if ' 200 ' in status:
                prop=propstat.find('{DAV:}prop');break
        if prop is None:continue
        rtype=prop.find('{DAV:}resourcetype')
        if rtype is not None and rtype.find('{DAV:}collection') is not None:continue
        size_text=str(prop.findtext('{DAV:}getcontentlength') or '').strip()
        try:size=int(size_text)
        except Exception:size=None
        result[name]={
            'size':size,
            'etag':str(prop.findtext('{DAV:}getetag') or '').strip(),
            'lastModified':str(prop.findtext('{DAV:}getlastmodified') or '').strip(),
        }
    return result


def _webdav_mkcol(url,username,password):
    code,_,_=_webdav_request('MKCOL',url,username,password,allowed={201,200,204,405})
    return code


def _webdav_get_json(url,username,password):
    code,body,_=_webdav_request('GET',url,username,password,allowed={200,404})
    if code==404:return {}
    try:return json.loads(body.decode('utf-8'))
    except Exception:raise RuntimeError('The cloud backup metadata is unreadable.')


def _webdav_put_json(url,obj,username,password):
    data=json.dumps(obj,indent=2,ensure_ascii=False).encode('utf-8')
    _webdav_request('PUT',url,username,password,data=data,headers={'Content-Type':'application/json; charset=utf-8'},allowed={200,201,204})


def _webdav_delete(url,username,password):
    _webdav_request('DELETE',url,username,password,allowed={200,204,404})


def _webdav_download(url,dest,username,password):
    dest=Path(dest);dest.parent.mkdir(parents=True,exist_ok=True)
    hdr={'Authorization':_webdav_auth_header(username,password),'User-Agent':f'StashLibrary/{STASHLIBRARY_VERSION}'}
    req=urllib.request.Request(url,headers=hdr,method='GET')
    try:
        with urllib.request.urlopen(req,timeout=120) as resp,dest.open('wb') as out:
            while True:
                chunk=resp.read(1024*1024)
                if not chunk:break
                out.write(chunk)
            out.flush()
            try:os.fsync(out.fileno())
            except Exception:pass
    except urllib.error.HTTPError as e:
        if e.code==404:raise RuntimeError('No cloud backup was found for this StashLibrary account.')
        raise RuntimeError(f'Cloud backup download returned HTTP {e.code}.')
    except urllib.error.URLError as e:raise RuntimeError(f'Could not reach the WebDAV server: {getattr(e,"reason",e)}')


def _webdav_sha256(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for chunk in iter(lambda:f.read(1024*1024),b''):h.update(chunk)
    return h.hexdigest()


def _webdav_catalogue_stats(path):
    path=Path(path)
    with sqlite3.connect(f'file:{path.as_posix()}?mode=ro&immutable=1',uri=True) as conn:
        row=conn.execute('PRAGMA quick_check').fetchone()
        if not row or str(row[0]).lower()!='ok':
            raise RuntimeError('The StashLibrary catalogue failed its SQLite integrity check.')
        tables={str(r[0]) for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        required={'nodes','files','catalogue_meta'}
        if not required.issubset(tables):
            raise RuntimeError('The StashLibrary catalogue is missing required tables.')
        nodes=int(conn.execute("SELECT COUNT(*) FROM nodes WHERE kind!='root'").fetchone()[0] or 0)
        files=int(conn.execute('SELECT COUNT(*) FROM files').fetchone()[0] or 0)
        bookmarks=int(conn.execute("SELECT COUNT(*) FROM nodes WHERE kind='bookmark'").fetchone()[0] or 0)
        library_id=_library_id_from_connection(conn)
        names=[str(r[0]) for r in conn.execute('SELECT storage_name FROM files').fetchall() if str(r[0] or '')]
    return {'nodes':nodes,'bookmarks':bookmarks,'files':files,'libraryId':library_id,'storageNames':names}


def _webdav_create_db_snapshot(expected=None):
    temp=DATA_DIR/f'webdav-catalogue-{uuid.uuid4().hex}.sqlite3'
    with DB_LOCK:
        conn=_db_connect()
        try:
            conn.commit();conn.execute('PRAGMA wal_checkpoint(FULL)');conn.commit()
            with sqlite3.connect(str(temp)) as dst:
                conn.backup(dst);dst.commit()
        finally:conn.close()
    try:
        stats=_webdav_catalogue_stats(temp)
        if expected:
            expected_files=int(expected.get('files') or 0);expected_nodes=int(expected.get('nodes') or 0)
            if stats['files']!=expected_files or stats['nodes']!=expected_nodes:
                raise RuntimeError(
                    f'StashLibrary refused to upload an incomplete catalogue snapshot '
                    f'(expected {expected_nodes} item(s) / {expected_files} file record(s), '
                    f'got {stats["nodes"]} / {stats["files"]}).'
                )
            if expected.get('libraryId') and stats.get('libraryId')!=expected.get('libraryId'):
                raise RuntimeError('StashLibrary refused to upload a catalogue snapshot with the wrong library identity.')
        return temp,stats
    except Exception:
        try:temp.unlink()
        except Exception:pass
        raise


def _webdav_local_inventory():
    root=bookmarks_path()
    if not root:raise RuntimeError('Choose your local StashLibrary folder first.')
    if not is_flat_layout():raise RuntimeError("Cloud Backup requires StashLibrary's current SQLite storage layout.")
    repair_self_contained_library_data(root)
    inv=_library_inventory_from_root(root)
    # Treat the database as authoritative. This prevents a cloud backup from
    # silently containing archive files that are not represented in the catalogue.
    stats=_webdav_catalogue_stats(inv['db'])
    # Never publish an apparently empty catalogue while ordinary archive files
    # are sitting in the Local Storage folder.  That is a strong signal that a
    # migration/repair is still needed, not a legitimate empty backup.
    if int(stats.get('files') or 0)==0 and int(stats.get('nodes') or 0)==0:
        physical=[p for p in Path(root).iterdir() if p.is_file() and not p.name.startswith('.local-bookmarks-')]
        if physical:
            raise RuntimeError(
                f'StashLibrary found {len(physical)} local archive file(s), but the local catalogue is empty. '
                'Cloud Backup was stopped so those files cannot be replaced by an empty backup. '
                'Open Diagnostics & Repair before trying again.'
            )
    return {'files':inv['files'],'db':inv['db'],'libraryId':inv['libraryId'],'stats':stats}


def _webdav_remote_urls(cfg):
    root=_webdav_url(cfg['url'],WEBDAV_REMOTE_ROOT)
    files=_webdav_url(root,WEBDAV_REMOTE_FILES)
    return root,files,_webdav_url(root,WEBDAV_STATE_NAME),_webdav_url(root,WEBDAV_DB_NAME)


def _webdav_prepare_remote(cfg):
    # Authenticate against the configured WebDAV root first, then create StashLibrary's
    # own backup folder if it does not already exist.
    _webdav_request('PROPFIND',cfg['url'],cfg['username'],cfg['password'],data=b'',headers={'Depth':'0'},allowed={200,207})
    root,files,_,_=_webdav_remote_urls(cfg)
    _webdav_mkcol(root,cfg['username'],cfg['password'])
    _webdav_mkcol(files,cfg['username'],cfg['password'])
    return root,files


def webdav_connect(provider,url,username,password):
    provider=str(provider or '').strip().lower()
    if provider not in {'koofr','pcloud-eu','pcloud-us','nextcloud','owncloud','other'}:
        raise RuntimeError('Choose a WebDAV cloud provider.')
    url=_webdav_normalize_url(url or _webdav_default_url(provider))
    username=str(username or '').strip();password=str(password or '')
    if not username:raise RuntimeError('Enter your WebDAV username or email address.')
    if not password:raise RuntimeError('Enter your WebDAV password or app password.')
    temp={'enabled':True,'provider':provider,'url':url,'username':username,'password':password}
    _webdav_prepare_remote(temp)
    _,_,state_url,_=_webdav_remote_urls(temp)
    existing=_webdav_get_json(state_url,username,password)
    if existing and existing.get('format')!=WEBDAV_FORMAT:
        raise RuntimeError('The existing StashLibrary WebDAV folder contains an unsupported backup format.')

    local=_webdav_local_inventory();local_names=set(local['files'])
    remote_files=existing.get('files') if isinstance(existing.get('files'),dict) else {}
    remote_names=set(remote_files)
    local_library_id=str(local.get('libraryId') or '')
    remote_library_id=str(existing.get('libraryId') or '').strip() if existing else ''
    same_library=bool(existing and local_library_id and remote_library_id and local_library_id==remote_library_id)
    # Manual-only cloud backup: connecting never uploads anything. The only
    # hard hold is the disaster-recovery case where an empty local library
    # could otherwise replace a non-empty cloud backup.
    local_empty=(int(local['stats'].get('nodes') or 0)==0 and len(local_names)==0)
    safety_hold=bool(existing and local_empty)

    c=read_config();c['webdav_backup_enabled']=True;c['webdav_provider']=provider;c['webdav_url']=url;c['webdav_username']=username
    c['webdav_secret']=_protect_webdav_secret(password);c['webdav_has_backup']=bool(existing);c['webdav_last_error']=''
    c['webdav_safety_hold']=safety_hold;c['webdav_existing_backup_same_library']=same_library
    c['webdav_remote_file_count']=len(remote_names);c['webdav_local_file_count']=len(local_names)
    c['webdav_manual_only']=True
    if existing and existing.get('updatedAt'):c['webdav_last_backup_at']=str(existing.get('updatedAt'))
    last_run=existing.get('lastRun') if isinstance(existing.get('lastRun'),dict) else {}
    if last_run:
        c['webdav_last_run_new']=int(last_run.get('new') or 0);c['webdav_last_run_updated']=int(last_run.get('updated') or 0)
        c['webdav_last_run_deleted']=int(last_run.get('deleted') or 0);c['webdav_last_run_unchanged']=int(last_run.get('unchanged') or 0)
        c['webdav_last_run_provider_confirmed']=bool(last_run.get('providerConfirmed'))
    else:
        c['webdav_last_run_new']=0;c['webdav_last_run_updated']=0;c['webdav_last_run_deleted']=0;c['webdav_last_run_unchanged']=0;c['webdav_last_run_provider_confirmed']=False
    write_config(c)
    return webdav_backup_info()


def webdav_disconnect():
    global WEBDAV_TIMER
    with WEBDAV_TIMER_LOCK:
        if WEBDAV_TIMER:
            try:WEBDAV_TIMER.cancel()
            except Exception:pass
            WEBDAV_TIMER=None
    c=read_config();c['webdav_backup_enabled']=False;c.pop('webdav_secret',None);c['webdav_last_error']='';write_config(c)
    return webdav_backup_info()


def webdav_backup_info():
    c=read_config();provider=str(c.get('webdav_provider') or '').strip().lower();enabled=bool(c.get('webdav_backup_enabled'))
    password=bool(_unprotect_webdav_secret(c.get('webdav_secret') or ''))
    connected=bool(enabled and provider and c.get('webdav_url') and c.get('webdav_username') and password)
    remote_count=int(c.get('webdav_remote_file_count') or 0)
    try:
        local_data=_webdav_local_inventory() if connected else None
        local_count=len(local_data['files']) if local_data else 0
        local_nodes=int(local_data['stats'].get('nodes') or 0) if local_data else 0
    except Exception:
        local_count=int(c.get('webdav_local_file_count') or 0);local_nodes=0
    safety_hold=bool(c.get('webdav_safety_hold')) if connected else False
    return {
        'enabled':enabled,'connected':connected,'manualOnly':True,
        'provider':provider,'providerLabel':_webdav_provider_label(provider),'url':str(c.get('webdav_url') or ''),'username':str(c.get('webdav_username') or ''),
        'lastBackupAt':str(c.get('webdav_last_backup_at') or ''),'lastError':str(c.get('webdav_last_error') or ''),
        'hasCloudBackup':bool(c.get('webdav_has_backup')),'backupActive':WEBDAV_BACKUP_LOCK.locked(),
        'safetyHold':safety_hold,'sameLibrary':bool(c.get('webdav_existing_backup_same_library')),
        'remoteFileCount':remote_count,'localFileCount':local_count,'localNodeCount':local_nodes,
        'canReplaceWithCurrent':bool(connected and not (safety_hold and local_count==0 and local_nodes==0)),
        'lastRunNew':int(c.get('webdav_last_run_new') or 0),
        'lastRunUpdated':int(c.get('webdav_last_run_updated') or 0),
        'lastRunDeleted':int(c.get('webdav_last_run_deleted') or 0),
        'lastRunUnchanged':int(c.get('webdav_last_run_unchanged') or 0),
        'lastRunProviderConfirmed':bool(c.get('webdav_last_run_provider_confirmed')),
        'canReconnect':False,
    }


def _webdav_record_error(error):
    try:
        c=read_config();c['webdav_last_error']=str(error);write_config(c)
    except Exception:pass


def webdav_backup_now(reason='manual',force_replace=True):
    """Create/update the WebDAV backup only after an explicit user action.

    Repeat backups are content-aware. StashLibrary uses the SHA-256 inventory already
    published in backup-state.json and the actual WebDAV folder listing to leave
    identical files untouched. If an older backup has incomplete metadata, the
    existing remote object is downloaded and hashed once rather than blindly
    uploaded again.
    """
    if not WEBDAV_BACKUP_LOCK.acquire(blocking=False):raise RuntimeError('A cloud backup is already in progress.')
    snapshot=None;remote_check=None;started=time.monotonic()
    try:
        # A successful new attempt supersedes any stale error shown from an
        # earlier run. If this attempt fails, the except block records the new
        # error immediately.
        try:
            c=read_config();c['webdav_last_error']='';write_config(c)
        except Exception:pass

        cfg=_webdav_config(require=True);_webdav_prepare_remote(cfg)
        local_data=_webdav_local_inventory();local=local_data['files'];local_stats=local_data['stats'];local_library_id=str(local_data.get('libraryId') or '')
        root_url,files_url,state_url,db_url=_webdav_remote_urls(cfg)
        remote=_webdav_get_json(state_url,cfg['username'],cfg['password'])
        if remote and remote.get('format')!=WEBDAV_FORMAT:raise RuntimeError('The existing StashLibrary WebDAV folder contains an unsupported backup format.')
        old_files=remote.get('files') if isinstance(remote.get('files'),dict) else {}
        remote_listing=_webdav_list_remote_files(files_url,cfg['username'],cfg['password'])
        remote_has_data=bool(old_files or remote.get('database') or remote_listing or _webdav_remote_size(db_url,cfg['username'],cfg['password']) is not None)
        local_empty=(int(local_stats.get('nodes') or 0)==0 and len(local)==0)
        if remote_has_data and local_empty:
            raise RuntimeError('Cloud backup protected: StashLibrary will not replace a non-empty cloud backup with an empty local library.')

        progress('Cloud Backup','verifying local catalogue','checking SQLite data',2,started)
        snapshot,snapshot_stats=_webdav_create_db_snapshot(local_stats)
        db_hash=_webdav_sha256(snapshot)
        if len(local)>0 and snapshot_stats['files']==0:
            raise RuntimeError('StashLibrary refused to upload an empty catalogue because this library contains archived files.')

        progress('Cloud Backup','comparing cloud backup','checking existing files',5,started)
        changed=[];new_files={};remote_hash_checks=0
        for name,entry in local.items():
            old=old_files.get(name) if isinstance(old_files.get(name),dict) else {}
            remote_meta=remote_listing.get(name) if isinstance(remote_listing.get(name),dict) else None
            remote_present=remote_meta is not None
            local_size=int(entry['size']);local_mtime=int(entry['mtimeNs'])
            old_sha=str(old.get('sha256') or '').strip()
            old_size=int(old.get('size') or -1)
            old_mtime=int(old.get('mtimeNs') or -1)
            remote_size=(remote_meta or {}).get('size') if remote_present else None

            digest=None;unchanged=False
            # Fast path: state has a trusted content hash, the remote object is
            # still present at the expected size, and the local file metadata is
            # unchanged. No file bytes need to be read or transferred.
            if old_sha and remote_present and old_size==local_size and (remote_size is None or int(remote_size)==local_size):
                if old_mtime==local_mtime:
                    unchanged=True;digest=old_sha
                else:
                    # Timestamps can change after copying/moving a library. Hash
                    # the local file before deciding it really changed.
                    digest=_webdav_sha256(entry['path'])
                    unchanged=(digest==old_sha)

            # Legacy/incomplete state: if a same-named remote object already
            # exists with the same size, compare content once instead of blindly
            # re-uploading it. The resulting hash is then written into the new
            # state, so future runs take the fast path above.
            elif remote_present and (remote_size is None or int(remote_size)==local_size):
                digest=_webdav_sha256(entry['path'])
                remote_digest,actual_remote_size=_webdav_remote_sha256(
                    _webdav_url(files_url,name),cfg['username'],cfg['password']
                )
                remote_hash_checks+=1
                unchanged=bool(remote_digest and actual_remote_size==local_size and remote_digest==digest)

            if unchanged:
                new_files[name]={
                    'size':local_size,'mtimeNs':local_mtime,'sha256':digest or old_sha,
                    'etag':str((remote_meta or {}).get('etag') or old.get('etag') or '')
                }
            else:
                if digest is None:digest=_webdav_sha256(entry['path'])
                new_files[name]={
                    'size':local_size,'mtimeNs':local_mtime,'sha256':digest,
                    'etag':str((remote_meta or {}).get('etag') or '')
                }
                changed.append((name,entry,remote_present))

        old_db=remote.get('database') if isinstance(remote.get('database'),dict) else {}
        old_db_hash=str(old_db.get('sha256') or '').strip()
        upload_db=(old_db_hash!=db_hash)
        # Older backup-state files may not contain a catalogue hash. Compare the
        # existing remote catalogue once before deciding to replace it.
        if upload_db and not old_db_hash and _webdav_remote_size(db_url,cfg['username'],cfg['password']) is not None:
            remote_db_hash,remote_db_size=_webdav_remote_sha256(db_url,cfg['username'],cfg['password'])
            if remote_db_hash==db_hash and remote_db_size==snapshot.stat().st_size:
                upload_db=False

        upload_bytes=sum(int(e['size']) for _,e,_ in changed)+(snapshot.stat().st_size if upload_db else 0)
        skipped=max(len(local)-len(changed),0)
        stale_remote=set(remote_listing)-set(new_files)
        new_count=sum(1 for _,_,was_remote in changed if not was_remote)
        updated_count=sum(1 for _,_,was_remote in changed if was_remote)
        progress(
            'Cloud Backup','comparing cloud backup',
            f'{skipped} unchanged · {len(changed)} to upload · {len(stale_remote)} to delete',6,started
        )

        sent_before=0
        for idx,(name,entry,was_remote) in enumerate(changed):
            size=max(int(entry['size']),1)
            def cb(sent,total,attempt,name=name,base=sent_before):
                denom=max(upload_bytes,1);pct=6+70*((base+sent)/denom)
                detail=f'{name} — {sent/(1024*1024):.1f} / {max(total,1)/(1024*1024):.1f} MB'
                if attempt>1:detail+=f' · retry {attempt}'
                progress('Cloud Backup','uploading files',detail,min(pct,76),started)
            remote_file_url=_webdav_url(files_url,name)
            _webdav_stream_put(entry['path'],remote_file_url,cfg['username'],cfg['password'],progress_cb=cb,max_attempts=3)
            remote_size=_webdav_remote_size(remote_file_url,cfg['username'],cfg['password'])
            if remote_size is not None and remote_size!=int(entry['size']):
                raise RuntimeError(f'The uploaded cloud file has the wrong size: {name}')
            # Refresh metadata for the just-written object when possible.
            try:
                listed=_webdav_list_remote_files(files_url,cfg['username'],cfg['password']).get(name) or {}
                if listed.get('etag'):new_files[name]['etag']=str(listed.get('etag'))
            except Exception:pass
            sent_before+=int(entry['size'])

        if upload_db:
            db_size=max(snapshot.stat().st_size,1)
            def dbcb(sent,total,attempt,base=sent_before):
                denom=max(upload_bytes,1);pct=6+70*((base+sent)/denom)
                detail=f'StashLibrary catalogue — {sent/(1024*1024):.1f} / {max(total,1)/(1024*1024):.1f} MB'
                if attempt>1:detail+=f' · retry {attempt}'
                progress('Cloud Backup','uploading catalogue',detail,min(pct,82),started)
            _webdav_stream_put(snapshot,db_url,cfg['username'],cfg['password'],progress_cb=dbcb,max_attempts=3)

        # Verify the actual remote SQLite file, not just the local snapshot.
        progress('Cloud Backup','verifying cloud catalogue','downloading verification copy',84,started)
        remote_check=DATA_DIR/f'webdav-verify-{uuid.uuid4().hex}.sqlite3'
        _webdav_download(db_url,remote_check,cfg['username'],cfg['password'])
        if _webdav_sha256(remote_check)!=db_hash:
            raise RuntimeError('The cloud catalogue did not match the verified local snapshot after upload.')
        remote_stats=_webdav_catalogue_stats(remote_check)
        for key in ('nodes','files','bookmarks'):
            if int(remote_stats.get(key) or 0)!=int(snapshot_stats.get(key) or 0):
                raise RuntimeError('The cloud catalogue was uploaded but its table contents did not match the local catalogue.')

        now=datetime.now(timezone.utc).astimezone().isoformat(timespec='seconds')
        state={'format':WEBDAV_FORMAT,'version':WEBDAV_FORMAT_VERSION,'updatedAt':now,'reason':'manual','libraryId':local_library_id,'files':new_files,
               'database':{'size':snapshot.stat().st_size,'sha256':db_hash,'nodes':snapshot_stats['nodes'],'bookmarks':snapshot_stats['bookmarks'],'files':snapshot_stats['files']}}
        progress('Cloud Backup','publishing verified backup','backup metadata',94,started)
        _webdav_put_json(state_url,state,cfg['username'],cfg['password'])
        published=_webdav_get_json(state_url,cfg['username'],cfg['password'])
        if str((published.get('database') or {}).get('sha256') or '')!=db_hash or set((published.get('files') or {}).keys())!=set(new_files.keys()):
            raise RuntimeError('StashLibrary could not verify the published cloud-backup metadata.')

        # Delete only files that physically exist remotely but no longer belong
        # to the local catalogue. This happens after the new database/state are
        # verified, so a failed deletion leaves only a harmless extra copy.
        deleted=0
        for name in stale_remote:
            _webdav_delete(_webdav_url(files_url,name),cfg['username'],cfg['password']);deleted+=1

        # Query the provider again after all writes/deletes. This is an actual
        # WebDAV PROPFIND against Koofr/pCloud/etc., not a count inferred only
        # from StashLibrary's local work queue. It confirms that every expected object
        # exists remotely at the expected size and every stale object is gone.
        progress('Cloud Backup','confirming cloud changes','querying the provider',97,started)
        final_listing={}
        for attempt in range(3):
            final_listing=_webdav_list_remote_files(files_url,cfg['username'],cfg['password'])
            if set(final_listing)==set(new_files):break
            if attempt<2:time.sleep(0.45*(attempt+1))
        missing=set(new_files)-set(final_listing);extras=set(final_listing)-set(new_files)
        if missing or extras:
            detail=[]
            if missing:detail.append(f'{len(missing)} expected file(s) missing')
            if extras:detail.append(f'{len(extras)} deleted file(s) still present')
            raise RuntimeError('The cloud provider did not confirm the final backup state: '+', '.join(detail)+'.')
        for name,meta in new_files.items():
            remote_size=(final_listing.get(name) or {}).get('size')
            if remote_size is not None and int(remote_size)!=int(meta.get('size') or 0):
                raise RuntimeError(f'The cloud provider reported the wrong final size for: {name}')

        # Add the server-confirmed transaction counts to the remote metadata so
        # reconnecting on another computer can display the same last-run result.
        state['lastRun']={'new':new_count,'updated':updated_count,'deleted':deleted,'unchanged':skipped,'providerConfirmed':True}
        _webdav_put_json(state_url,state,cfg['username'],cfg['password'])
        final_state=_webdav_get_json(state_url,cfg['username'],cfg['password'])
        if not isinstance(final_state.get('lastRun'),dict) or not bool(final_state['lastRun'].get('providerConfirmed')):
            raise RuntimeError('StashLibrary could not confirm the final cloud-backup summary on the provider.')

        c=read_config();c['webdav_last_backup_at']=now;c['webdav_last_error']='';c['webdav_has_backup']=True;c['webdav_safety_hold']=False;c['webdav_existing_backup_same_library']=True;c['webdav_remote_file_count']=len(new_files);c['webdav_local_file_count']=len(new_files);c['webdav_manual_only']=True
        c['webdav_last_run_new']=new_count;c['webdav_last_run_updated']=updated_count;c['webdav_last_run_deleted']=deleted;c['webdav_last_run_unchanged']=skipped;c['webdav_last_run_provider_confirmed']=True;write_config(c)
        summary=f'{new_count} new · {updated_count} updated · {deleted} deleted · {skipped} unchanged · provider confirmed'
        progress('Cloud Backup','finished',summary,100,started)
        return {
            'backedUp':True,'updatedAt':now,'provider':cfg['provider'],'providerLabel':_webdav_provider_label(cfg['provider']),
            'files':len(new_files),'nodes':snapshot_stats['nodes'],'databaseVerified':True,'providerConfirmed':True,
            'newFiles':new_count,'updatedFiles':updated_count,'uploadedFiles':len(changed),'unchangedFiles':skipped,'deletedFiles':deleted,'databaseUploaded':bool(upload_db),
            'legacyRemoteHashChecks':remote_hash_checks,
        }
    except Exception as e:
        _webdav_record_error(e);progress('Cloud Backup','failed',str(e),None,started);raise
    finally:
        for q in (snapshot,remote_check):
            if q:
                try:Path(q).unlink()
                except Exception:pass
        WEBDAV_BACKUP_LOCK.release()


def _webdav_scheduled_run():
    # v0.10.179: Cloud Backup is deliberately manual-only.
    global WEBDAV_TIMER
    with WEBDAV_TIMER_LOCK:WEBDAV_TIMER=None


def schedule_webdav_backup(delay=None):
    # Compatibility no-op for older call sites. No background/cloud upload is
    # ever started in v0.10.179; only the explicit Back Up Now command writes.
    return


def webdav_replace_with_current():
    cfg=_webdav_config(require=True);_webdav_prepare_remote(cfg)
    _,_,state_url,_=_webdav_remote_urls(cfg)
    remote=_webdav_get_json(state_url,cfg['username'],cfg['password'])
    old_files=remote.get('files') if isinstance(remote.get('files'),dict) else {}
    local=_webdav_local_inventory()
    if old_files and not local:
        raise RuntimeError('StashLibrary will not replace a non-empty cloud backup with an empty local library.')
    result=webdav_backup_now('manual',force_replace=True)
    return {**result,**webdav_backup_info()}


def _prepare_webdav_restore_destination(root):
    """Pin Cloud Restore to StashLibrary's current self-contained disk layout.

    A disaster-recovery restore is commonly started against a genuinely empty
    directory.  In that case there may be no catalogue for the normal migration
    code to inspect, so older layout markers must never decide where restored
    payloads are written.  The visible StashLibrary folder is always the archive root;
    only SQLite/recovery data belongs in .stashlibrary-data.
    """
    root=Path(root);root.mkdir(parents=True,exist_ok=True)
    data=root/UNIFIED_DATA_DIR_NAME;data.mkdir(parents=True,exist_ok=True)
    c=read_config()
    c['storage_layout']='flat-sqlite-v2'
    c['internal_data_layout']='library-local-v3'
    c['internal_data_path']=str(data)
    c['storage_root_layout']='self-contained-v1'
    c['storage_sync_mode']='local'
    write_config(c)
    return root,data/DATABASE_NAME


def webdav_restore():
    """Restore the connected WebDAV backup without re-downloading identical files.

    The cloud catalogue remains authoritative, but archive payloads are restored
    incrementally.  Files whose local SHA-256 already matches the cloud snapshot
    are left in place; only missing/changed payloads are downloaded.  Local files
    represented by the current catalogue but absent from the cloud snapshot are
    removed only after the cloud catalogue and every required payload have been
    verified.

    Cloud Restore also pins a completely empty destination to the modern
    self-contained layout before any paths are calculated.  This prevents the
    catalogue from being restored successfully while payloads are accidentally
    written to a retired hidden archive directory.
    """
    global undo_stack,redo_stack
    if WEBDAV_BACKUP_LOCK.locked():raise RuntimeError('Wait for the cloud backup to finish before restoring it.')
    cfg=_webdav_config(require=True);_webdav_prepare_remote(cfg)
    _,files_url,state_url,db_url=_webdav_remote_urls(cfg);state=_webdav_get_json(state_url,cfg['username'],cfg['password'])
    if not state:raise RuntimeError('No StashLibrary cloud backup was found.')
    if state.get('format')!=WEBDAV_FORMAT:raise RuntimeError('The cloud backup format is not supported by this version of StashLibrary.')
    files=state.get('files') if isinstance(state.get('files'),dict) else {}
    root=bookmarks_path()
    if not root:raise RuntimeError('Choose your local StashLibrary folder first.')
    repair_self_contained_library_data(root)
    arc,live_db=_prepare_webdav_restore_destination(root)
    stage=DATA_DIR/f'webdav-restore-{uuid.uuid4().hex}';stage.mkdir(parents=True,exist_ok=True)
    stage_files=stage/'files';stage_files.mkdir(parents=True,exist_ok=True);stage_db=stage/WEBDAV_DB_NAME
    progress('Cloud Restore','downloading catalogue','StashLibrary catalogue',2)
    downloaded=[];reused=[];local_hash_checks=0
    try:
        # The catalogue is small and authoritative, so always fetch and verify it
        # before touching the local library.
        _webdav_download(db_url,stage_db,cfg['username'],cfg['password'])
        expected_db=str((state.get('database') or {}).get('sha256') or '')
        if expected_db and _webdav_sha256(stage_db)!=expected_db:raise RuntimeError('The downloaded cloud catalogue failed its checksum check.')
        cloud_stats=_webdav_catalogue_stats(stage_db)
        expected_stats=state.get('database') if isinstance(state.get('database'),dict) else {}
        for key in ('nodes','files','bookmarks'):
            if key in expected_stats and int(expected_stats.get(key) or 0)!=int(cloud_stats.get(key) or 0):
                raise RuntimeError('The downloaded cloud catalogue table contents did not match the published backup metadata.')

        names=list(files)
        for name in names:
            if Path(name).name!=name:raise RuntimeError('Cloud backup contains an unsafe filename.')

        # A valid cloud catalogue may only reference objects listed by the cloud
        # backup state.  Reject incomplete metadata before any local changes.
        required_names=set(cloud_stats.get('storageNames') or [])
        missing_state=sorted(required_names-set(names))
        if missing_state:
            preview=', '.join(missing_state[:3]);more=f' (+{len(missing_state)-3} more)' if len(missing_state)>3 else ''
            raise RuntimeError(f'Cloud backup is incomplete: {len(missing_state)} catalogue file(s) are absent from backup-state.json: {preview}{more}')

        # Compare each cloud object with the current local payload.  Current
        # backups carry SHA-256 fingerprints, so unchanged files require no
        # network transfer.  Older backups without hashes are downloaded once
        # because size/name alone is not strong enough to safely skip a restore.
        to_download=[]
        total=max(len(names),1)
        for i,name in enumerate(names):
            meta=files.get(name) if isinstance(files.get(name),dict) else {}
            expected_sha=str(meta.get('sha256') or '').strip()
            try:expected_size=int(meta.get('size')) if meta.get('size') is not None else None
            except Exception:expected_size=None
            local=arc/name
            same=False
            if local.is_file() and expected_sha:
                try:
                    if expected_size is None or int(local.stat().st_size)==expected_size:
                        local_hash_checks+=1
                        same=(_webdav_sha256(local)==expected_sha)
                except Exception:same=False
            if same:
                reused.append(name)
            else:
                to_download.append(name)
            progress('Cloud Restore','comparing local files',f'{i+1}/{len(names)} checked',5+15*((i+1)/total))

        progress('Cloud Restore','comparing local files',f'{len(reused)} unchanged · {len(to_download)} to download',20)
        dl_total=max(len(to_download),1)
        for i,name in enumerate(to_download):
            progress('Cloud Restore','downloading changed files',name,20+60*((i+1)/dl_total))
            dst=stage_files/name
            _webdav_download(_webdav_url(files_url,name),dst,cfg['username'],cfg['password'])
            meta=files.get(name) if isinstance(files.get(name),dict) else {}
            digest=str(meta.get('sha256') or '').strip()
            try:expected_size=int(meta.get('size')) if meta.get('size') is not None else None
            except Exception:expected_size=None
            if expected_size is not None and int(dst.stat().st_size)!=expected_size:
                raise RuntimeError(f'The downloaded file has the wrong size: {name}')
            if digest and _webdav_sha256(dst)!=digest:
                raise RuntimeError(f'The downloaded file failed its checksum check: {name}')
            downloaded.append(name)

        # Every file referenced by the restored catalogue must now either be a
        # verified unchanged local object or a verified staged download.
        verified=set(reused)|set(downloaded)
        missing=sorted(required_names-verified)
        if missing:
            raise RuntimeError(f'Cloud backup is incomplete: {len(missing)} archived file(s) could not be verified.')

        rollback=DATA_DIR/f'webdav-restore-rollback-{uuid.uuid4().hex}';rollback.mkdir(parents=True,exist_ok=True)
        old_names=[];touched=set();created=[]
        try:
            with DB_LOCK:
                # Flush the current WAL before taking the rollback copy.
                try:
                    current=_db_connect();current.commit();current.execute('PRAGMA wal_checkpoint(FULL)');current.commit();current.close()
                except Exception:pass
                if Path(live_db).exists():
                    rbdb=rollback/WEBDAV_DB_NAME
                    src=sqlite3.connect(str(live_db),timeout=20);dst=sqlite3.connect(str(rbdb),timeout=20)
                    try:src.backup(dst);dst.commit()
                    finally:dst.close();src.close()
                try:
                    with sqlite3.connect(str(live_db)) as old:
                        old_names=[str(r[0]) for r in old.execute('SELECT storage_name FROM files').fetchall() if str(r[0] or '')]
                except Exception:old_names=[]

                deleted=set(old_names)-set(names)
                touched=set(downloaded)|deleted
                rbfiles=rollback/'files';rbfiles.mkdir(parents=True,exist_ok=True)
                for name in touched:
                    src=arc/name
                    if src.is_file():shutil.copy2(src,rbfiles/name)
                    elif name in downloaded:created.append(name)

                # Only changed/missing cloud files are written locally. Verified
                # identical files remain untouched, including their timestamps.
                for name in downloaded:shutil.copy2(stage_files/name,arc/name)
                for name in deleted:
                    try:(arc/name).unlink()
                    except FileNotFoundError:pass

                # Use the same SQLite-safe install path as catalogue migration so
                # Windows open-file semantics cannot turn a restore into WinError 32.
                _install_verified_catalogue_copy(stage_db,live_db)

                # Do not report success merely because the SQLite records were
                # installed.  A restore is complete only when every catalogue
                # payload physically exists in the visible StashLibrary folder and its
                # size/hash still matches the cloud snapshot.  Keeping this check
                # inside the rollback transaction prevents the exact failure mode
                # where the tree appears populated but opening an item reports a
                # catalogue/file mismatch.
                installed_stats=_webdav_catalogue_stats(live_db)
                for key in ('nodes','files','bookmarks'):
                    if int(installed_stats.get(key) or 0)!=int(cloud_stats.get(key) or 0):
                        raise RuntimeError('The restored local catalogue did not match the verified cloud catalogue.')
                if str(cloud_stats.get('libraryId') or '') and str(installed_stats.get('libraryId') or '')!=str(cloud_stats.get('libraryId') or ''):
                    raise RuntimeError('The restored local catalogue has the wrong library identity.')

                final_missing=[]
                for name in required_names:
                    local_path=arc/name
                    if not local_path.is_file():
                        final_missing.append(name);continue
                    meta=files.get(name) if isinstance(files.get(name),dict) else {}
                    try:expected_size=int(meta.get('size')) if meta.get('size') is not None else None
                    except Exception:expected_size=None
                    expected_sha=str(meta.get('sha256') or '').strip()
                    if expected_size is not None and int(local_path.stat().st_size)!=expected_size:
                        raise RuntimeError(f'The restored local file has the wrong size: {name}')
                    if expected_sha and _webdav_sha256(local_path)!=expected_sha:
                        raise RuntimeError(f'The restored local file failed its checksum check: {name}')
                if final_missing:
                    preview=', '.join(final_missing[:3]);more=f' (+{len(final_missing)-3} more)' if len(final_missing)>3 else ''
                    raise RuntimeError(
                        f'Cloud Restore could not populate the local StashLibrary folder: '
                        f'{len(final_missing)} archived file(s) are still missing: {preview}{more}'
                    )
            shutil.rmtree(rollback,ignore_errors=True)
        except Exception:
            try:
                with DB_LOCK:
                    rbfiles=rollback/'files'
                    # Remove files that were newly created during the failed
                    # restore, then put back every overwritten/deleted payload.
                    for name in created:
                        try:(arc/name).unlink(missing_ok=True)
                        except Exception:pass
                    if rbfiles.is_dir():
                        for q in rbfiles.iterdir():
                            if q.is_file():shutil.copy2(q,arc/q.name)
                    if (rollback/WEBDAV_DB_NAME).is_file():
                        _install_verified_catalogue_copy(rollback/WEBDAV_DB_NAME,live_db)
            except Exception:pass
            raise

        undo_stack=[];redo_stack=[];_save_history()
        try:_with_db(lambda conn:_flat_write_recovery(conn))
        except Exception:pass
        c=read_config();c['webdav_last_error']='';c['webdav_last_backup_at']=str(state.get('updatedAt') or c.get('webdav_last_backup_at') or '');c['webdav_has_backup']=True;c['webdav_safety_hold']=False;c['webdav_existing_backup_same_library']=True;c['webdav_remote_file_count']=len(files);c['webdav_local_file_count']=len(files);write_config(c)
        summary=f'{len(downloaded)} downloaded · {len(reused)} unchanged'
        progress('Cloud Restore','finished',summary,100)
        return {
            'restored':True,'updatedAt':str(state.get('updatedAt') or ''),'files':len(files),
            'downloadedFiles':len(downloaded),'unchangedFiles':len(reused),'localHashChecks':local_hash_checks,
        }
    finally:shutil.rmtree(stage,ignore_errors=True)


# ----------------------------- cloud folder sync -----------------------------

# Retained only for compatibility with the retired v0.10.164-168 desktop-folder
# sync model. Modern StashLibrary uses Local Storage plus optional direct WebDAV backup.

def storage_sync_mode():
    c=read_config(); mode=str(c.get('storage_sync_mode') or '').strip().lower()
    if mode in {'local','synced'}:return mode
    return 'synced' if c.get('cloud_sync_enabled') else 'local'

def cloud_sync_path():
    return bookmarks_path() if storage_sync_mode()=='synced' else None


def _cloud_device_id():
    c=read_config(); did=str(c.get('cloud_device_id') or '').strip()
    if not did:
        did=uuid.uuid4().hex
        c['cloud_device_id']=did; write_config(c)
    return did


def _cloud_device_name():
    return os.environ.get('COMPUTERNAME') or os.environ.get('HOSTNAME') or 'This computer'


def _cloud_validate_root(path):
    p=Path(path).resolve(strict=False)
    p.mkdir(parents=True,exist_ok=True)
    if not p.is_dir():raise RuntimeError('The selected StashLibrary folder is unavailable.')
    return p


def _cloud_state_path(sync_root=None):
    r=Path(sync_root) if sync_root else cloud_sync_path()
    return (r/CLOUD_META_DIR/CLOUD_STATE_NAME) if r else None


def _cloud_read_state(sync_root=None):
    p=_cloud_state_path(sync_root)
    if not p or not p.exists():return {}
    try:
        data=json.loads(p.read_text(encoding='utf-8'))
        return data if isinstance(data,dict) else {}
    except Exception:return {}


def _cloud_write_state(sync_root,state):
    md=Path(sync_root)/CLOUD_META_DIR; md.mkdir(parents=True,exist_ok=True)
    tmp=md/(CLOUD_STATE_NAME+'.tmp')
    tmp.write_text(json.dumps(state,indent=2,ensure_ascii=False),encoding='utf-8')
    os.replace(tmp,md/CLOUD_STATE_NAME)


def _cloud_live_inventory(root):
    root=Path(root); out={}
    if not root.exists():return out
    for p in root.rglob('*'):
        if not p.is_file():continue
        try:relp=p.relative_to(root)
        except Exception:continue
        if relp.parts and relp.parts[0] in {SYSTEM_DIR_NAME,UNIFIED_DATA_DIR_NAME,LEGACY_UNIFIED_DATA_DIR_NAME,CLOUD_META_DIR}:continue
        if p.name.startswith('.local-bookmarks-'):continue
        try:
            st=p.stat(); out[relp.as_posix()]={'size':int(st.st_size),'mtimeNs':int(st.st_mtime_ns)}
        except Exception:continue
    return out


def _cloud_copy_user_files(source,target):
    source=Path(source);target=Path(target);target.mkdir(parents=True,exist_ok=True)
    if _same_fs_path(source,target):return 0
    return _safe_copy_contents(source,target,{SYSTEM_DIR_NAME,UNIFIED_DATA_DIR_NAME,LEGACY_UNIFIED_DATA_DIR_NAME,CLOUD_META_DIR})


def _migrate_legacy_cloud_archive(root):
    """Flatten the v0.10.163 ``archive`` mirror into the selected root."""
    root=Path(root); legacy=root/CLOUD_MIRROR_DIR
    if not legacy.exists() or not legacy.is_dir():return 0
    # Only treat it as the old mirror when StashLibrary cloud metadata also exists.
    if not (root/CLOUD_META_DIR/CLOUD_STATE_NAME).exists():return 0
    copied=_safe_copy_contents(legacy,root,set())
    for src in legacy.rglob('*'):
        if src.is_file():
            dst=root/src.relative_to(legacy)
            if not dst.is_file() or not _files_identical(src,dst):
                raise RuntimeError('Could not verify the old cloud mirror while converting it to the single-folder layout.')
    shutil.rmtree(legacy)
    return copied


def _verify_library_database(root):
    db=Path(root)/UNIFIED_DATA_DIR_NAME/DATABASE_NAME
    if not db.is_file():return False
    try:
        with sqlite3.connect(f'file:{db.as_posix()}?mode=ro&immutable=1',uri=True) as conn:
            row=conn.execute('PRAGMA quick_check').fetchone()
            return bool(row and str(row[0]).lower()=='ok')
    except Exception:return False


def _copy_self_contained_library(source,target):
    source=Path(source);target=Path(target);target.mkdir(parents=True,exist_ok=True)
    if _same_fs_path(source,target):return
    # Copy ordinary archive payloads first. Old cloud-sync metadata is deliberately
    # excluded because WebDAV Cloud Backup supersedes that model.
    _safe_copy_contents(source,target,{SYSTEM_DIR_NAME,UNIFIED_DATA_DIR_NAME,LEGACY_UNIFIED_DATA_DIR_NAME,CLOUD_META_DIR})
    source_data=internal_data_dir(root=source,create=False)
    target_data=target/UNIFIED_DATA_DIR_NAME
    if source_data and Path(source_data).exists():_copy_internal_data_safely(source_data,target_data)


def choose_storage_folder(mode='local'):
    # v0.10.171 exposes one local library folder. ``mode`` is accepted only for
    # compatibility with v0.10.165-168 Firefox builds still talking to a newer helper.
    # Choosing a different folder SWITCHES libraries; it does not move/copy the
    # current library into that folder. An empty folder is a valid empty library.
    current=bookmarks_path();initial=str(current or default_library_path())
    chosen=_win_choose_folder('Choose your StashLibrary storage folder',initial)
    if not chosen:return None
    target=_cloud_validate_root(Path(chosen));target.mkdir(parents=True,exist_ok=True)
    c=read_config()

    if current and _same_fs_path(current,target):
        # Older upgraded libraries may still need one repair pass. A folder that
        # was explicitly selected already must remain authoritative even if empty.
        if not _is_explicit_library_root(target,c):
            try:repair_self_contained_library_data(target)
            except Exception:pass
        c=read_config();c['bookmarks_path']=str(target);c['cloud_sync_path']=str(target)
        c['explicit_library_root']=str(target);c['explicit_library_selected_at']=capture_timestamp()
        c['internal_data_layout']='library-local-v3';c['internal_data_path']=str(target/UNIFIED_DATA_DIR_NAME)
        c['storage_sync_mode']='local';c['cloud_sync_enabled']=False;c['cloud_sync_model']='retired-v0.10.169';c['storage_root_layout']='self-contained-v1'
        write_config(c);internal_data_dir(create=True);return str(target)

    # Adopt an existing StashLibrary library as-is. Otherwise require an empty folder
    # and initialise a brand-new empty library there.
    target_has_library=_verify_library_database(target)
    if not target_has_library:
        existing=[x for x in target.iterdir() if x.name not in {CLOUD_META_DIR}]
        if existing:
            raise RuntimeError('Choose an empty folder or an existing StashLibrary folder.')

    c=read_config()
    if current:c['previous_bookmarks_path']=str(current)
    c['bookmarks_path']=str(target);c['cloud_sync_path']=str(target)
    c['explicit_library_root']=str(target);c['explicit_library_selected_at']=capture_timestamp()
    c['internal_data_layout']='library-local-v3';c['internal_data_path']=str(target/UNIFIED_DATA_DIR_NAME)
    c['storage_sync_mode']='local';c['cloud_sync_enabled']=False;c['cloud_sync_model']='retired-v0.10.169';c['storage_root_layout']='self-contained-v1'
    c.pop('cloud_last_sync_error',None);write_config(c)
    internal_data_dir(create=True)

    if not target_has_library:
        conn=_db_connect()
        try:
            conn.execute("INSERT OR REPLACE INTO catalogue_meta(key,value) VALUES('library_origin',?)",('user-selected-new',))
            conn.execute("INSERT OR REPLACE INTO catalogue_meta(key,value) VALUES('selected_root',?)",(str(target),))
            conn.commit()
        finally:conn.close()

    global undo_stack,redo_stack
    undo_stack,redo_stack=_load_history()
    return str(target)

def choose_cloud_sync_folder():
    return choose_storage_folder('synced')

def _cloud_publish_database(sync_root):
    source=database_file()
    if not source or not source.exists():raise RuntimeError('StashLibrary database is not available yet.')
    temp_local=DATA_DIR/f'cloud-snapshot-{uuid.uuid4().hex}.sqlite3'
    final_dir=Path(sync_root)/CLOUD_META_DIR; final_dir.mkdir(parents=True,exist_ok=True)
    staged=final_dir/(CLOUD_DB_NAME+'.tmp'); final=final_dir/CLOUD_DB_NAME
    try:
        with DB_LOCK:
            src=sqlite3.connect(str(source)); dst=sqlite3.connect(str(temp_local))
            try:src.backup(dst);dst.commit()
            finally:dst.close();src.close()
        chk=sqlite3.connect(str(temp_local))
        try:
            row=chk.execute('PRAGMA integrity_check').fetchone()
            if not row or str(row[0]).lower()!='ok':raise RuntimeError('Cloud database snapshot failed SQLite integrity check.')
        finally:chk.close()
        shutil.copy2(temp_local,staged); os.replace(staged,final)
    finally:
        try:temp_local.unlink(missing_ok=True)
        except Exception:pass
        try:staged.unlink(missing_ok=True)
        except Exception:pass
    return final


def _cloud_preflight_mutation():
    root=cloud_sync_path()
    if not root or not Path(root).exists():return
    state=_cloud_read_state(root)
    if not state:return
    cloud_rev=int(state.get('revision') or 0)
    last_seen=int(read_config().get('cloud_last_seen_revision') or 0)
    other=str(state.get('deviceId') or '') not in {'',_cloud_device_id()}
    if other and cloud_rev>last_seen:
        raise RuntimeError(f"A newer cloud copy is available from {state.get('deviceName') or 'another computer'}. Use Restore From Cloud before making changes on this computer.")


def cloud_sync_now(reason='manual'):
    with CLOUD_SYNC_LOCK:
        root=cloud_sync_path()
        if not root:raise RuntimeError('Choose a StashLibrary folder first.')
        root=_cloud_validate_root(root)
        _migrate_legacy_cloud_archive(root)
        state_before=_cloud_read_state(root)
        cloud_rev=int(state_before.get('revision') or 0)
        c=read_config();last_seen=int(c.get('cloud_last_seen_revision') or 0)
        other=str(state_before.get('deviceId') or '') not in {'',_cloud_device_id()}
        if other and cloud_rev>last_seen:
            raise RuntimeError(f"A newer cloud copy is available from {state_before.get('deviceName') or 'another computer'}. Use Restore From Cloud before syncing from this computer.")
        dbfile=_cloud_publish_database(root)
        inventory=_cloud_live_inventory(root)
        revision=max(cloud_rev,last_seen)+1
        state={
            'format':'StashLibrary cloud folder','formatVersion':2,'revision':revision,
            'updatedAt':datetime.now(timezone.utc).isoformat(timespec='seconds'),
            'deviceId':_cloud_device_id(),'deviceName':_cloud_device_name(),
            'stashlibraryVersion':STASHLIBRARY_VERSION,'schemaVersion':DATABASE_SCHEMA_VERSION,
            'archiveFiles':len(inventory),'database':f'{CLOUD_META_DIR}/{CLOUD_DB_NAME}',
            'reason':str(reason or 'sync')
        }
        _cloud_write_state(root,state)
        c=read_config();c['cloud_last_seen_revision']=revision;c['cloud_last_sync_at']=state['updatedAt'];c['cloud_last_sync_error']='';write_config(c)
        return {'synced':True,'path':str(root),'revision':revision,'files':len(inventory),'updatedAt':state['updatedAt'],'database':str(dbfile)}


def _cloud_record_error(message):
    try:
        c=read_config();c['cloud_last_sync_error']=str(message);write_config(c)
    except Exception:pass


def _cloud_scheduled_run():
    global CLOUD_SYNC_TIMER
    with CLOUD_SYNC_TIMER_LOCK:CLOUD_SYNC_TIMER=None
    try:cloud_sync_now('automatic')
    except Exception as e:_cloud_record_error(e)


def schedule_cloud_sync():
    global CLOUD_SYNC_TIMER
    if not cloud_sync_path():return
    with CLOUD_SYNC_TIMER_LOCK:
        if CLOUD_SYNC_TIMER:
            try:CLOUD_SYNC_TIMER.cancel()
            except Exception:pass
        CLOUD_SYNC_TIMER=threading.Timer(CLOUD_SYNC_DEBOUNCE_SECONDS,_cloud_scheduled_run)
        CLOUD_SYNC_TIMER.daemon=True;CLOUD_SYNC_TIMER.start()


def cloud_sync_info():
    c=read_config(); mode=storage_sync_mode(); library=bookmarks_path(); p=library if mode=='synced' else None
    state=_cloud_read_state(p) if p else {}
    cloud_rev=int(state.get('revision') or 0); last_seen=int(c.get('cloud_last_seen_revision') or 0)
    different=bool(state.get('deviceId') and state.get('deviceId')!=_cloud_device_id()) if p else False
    newer=different and cloud_rev>last_seen
    db=(Path(p)/CLOUD_META_DIR/CLOUD_DB_NAME) if p else None
    return {
        'configured':bool(library),'enabled':mode=='synced','mode':mode,'path':str(library or ''),'libraryPath':str(library or ''),
        'revision':cloud_rev,'lastSeenRevision':last_seen,'cloudNewer':newer,
        'cloudDevice':state.get('deviceName') or '','updatedAt':state.get('updatedAt') or '',
        'lastSyncAt':c.get('cloud_last_sync_at') or '','lastError':c.get('cloud_last_sync_error') or '',
        'deviceName':_cloud_device_name(),'hasCloudCopy':bool(mode=='synced' and state and db and db.is_file())
    }

def restore_from_cloud():
    global undo_stack,redo_stack
    with CLOUD_SYNC_LOCK, DB_LOCK:
        root=cloud_sync_path()
        if not root:raise RuntimeError('Choose a StashLibrary folder first.')
        root=_cloud_validate_root(root)
        _migrate_legacy_cloud_archive(root)
        state=_cloud_read_state(root)
        if not state:raise RuntimeError('No StashLibrary cloud copy was found in this folder.')
        cloud_db=root/CLOUD_META_DIR/CLOUD_DB_NAME
        if not cloud_db.exists():raise RuntimeError('Cloud database snapshot is missing.')
        chk=sqlite3.connect(str(cloud_db))
        try:
            row=chk.execute('PRAGMA integrity_check').fetchone()
            if not row or str(row[0]).lower()!='ok':raise RuntimeError('Cloud database snapshot is corrupt; local data was not changed.')
            # Do not restore until the desktop cloud client has downloaded every
            # archived file referenced by the snapshot.
            try:
                names=[str(r[0]) for r in chk.execute("SELECT storage_name FROM files WHERE storage_name<>''").fetchall()]
            except Exception:names=[]
            missing=[name for name in names if not (root/name).is_file()]
            if missing:
                preview=', '.join(missing[:3]);more=f' (+{len(missing)-3} more)' if len(missing)>3 else ''
                raise RuntimeError(f'Cloud files are still missing from this computer: {preview}{more}. Let your cloud app finish syncing, then try Restore From Cloud again.')
        finally:chk.close()

        live_db=database_file();tmpdb=DATA_DIR/f'cloud-restore-{uuid.uuid4().hex}.sqlite3'
        src=sqlite3.connect(str(cloud_db));dst=sqlite3.connect(str(tmpdb))
        try:src.backup(dst);dst.commit()
        finally:dst.close();src.close()
        check=sqlite3.connect(str(tmpdb))
        try:
            row=check.execute('PRAGMA integrity_check').fetchone()
            if not row or str(row[0]).lower()!='ok':raise RuntimeError('Restored SQLite copy failed integrity check.')
        finally:check.close()
        live_db.parent.mkdir(parents=True,exist_ok=True)
        for suffix in ('-wal','-shm'):
            try:Path(str(live_db)+suffix).unlink(missing_ok=True)
            except Exception:pass
        os.replace(tmpdb,live_db)
        undo_stack=[];redo_stack=[];_save_history()
        c=read_config();rev=int(state.get('revision') or 0);c['cloud_last_seen_revision']=rev;c['cloud_last_sync_at']=state.get('updatedAt') or '';c['cloud_last_sync_error']='';c.pop('cloud_single_folder_restore_required',None);write_config(c)
        return {'restored':True,'revision':rev,'files':len(names),'fromDevice':state.get('deviceName') or ''}


# ----------------------------- backups ---------------------------------

BACKUP_INCOMPLETE_MARKER='.stashlibrary-backup-incomplete'
BACKUP_CANCEL_EVENT=threading.Event()
BACKUP_RUN_LOCK=threading.Lock()

class BackupCancelled(RuntimeError):
    pass

def _check_backup_cancelled():
    if BACKUP_CANCEL_EVENT.is_set():raise BackupCancelled('Backup cancelled by user.')


def _read_zip_backup_info(path):
    """Read only StashLibrary's tiny metadata member from a ZIP without unpacking it."""
    try:
        p=Path(path)
        if not p.is_file() or not zipfile.is_zipfile(p):return None
        with zipfile.ZipFile(p,'r') as z:
            if 'backup-info.json' not in z.namelist():return None
            info=json.loads(z.read('backup-info.json').decode('utf-8'))
        return info if isinstance(info,dict) else None
    except Exception:return None


def _finished_backup_items(root=None):
    """Return completed StashLibrary ZIP backups, newest first.

    This is discovery only. StashLibrary never deletes completed backups automatically;
    users manage old backup ZIPs themselves.
    """
    b=Path(root) if root else backups_path()
    if not b or not b.exists():return []
    items=[]
    try:
        for pattern in ('StashLibrary-backup-*.zip','StashLibrary-backup-*.zip'):
            for p in b.glob(pattern):
                info=_read_zip_backup_info(p)
                if not info:continue
                if str(info.get('format') or '')!='StashLibrary backup':continue
                if str(info.get('backupType') or '').lower()!='zip':continue
                items.append(p)
    except Exception:pass
    return sorted(items,key=lambda p:p.stat().st_mtime if p.exists() else 0,reverse=True)

# Folder backups preserve the visible StashLibrary hierarchy. Individual Windows path
# components still need to be filesystem-safe. Extremely long *whole* paths are
# shortened in place rather than moved to a special _long-paths directory, so a
# backup remains intuitive to browse in Explorer. The SQLite snapshot preserves
# the exact original StashLibrary names/hierarchy for restore.
MAX_BACKUP_COMPONENT=180
MAX_FOLDER_BACKUP_REL=320

def _backup_safe_component(value, fallback='Untitled'):
    """Return a Windows-safe visible folder/file component for folder backups."""
    text=str(value or fallback).strip() or fallback
    text=re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', text).rstrip(' .')
    if not text:text=fallback
    stem=text.split('.')[0].upper()
    if stem in {'CON','PRN','AUX','NUL',*(f'COM{i}' for i in range(1,10)),*(f'LPT{i}' for i in range(1,10))}:
        text='_'+text
    return text[:MAX_BACKUP_COMPONENT].rstrip(' .') or fallback

def _folder_backup_rel(rel_parts,node_id,kind,ext=''):
    """Keep the normal hierarchy and shorten only the component that overflows.

    Unlike the old ZIP-specific layout this never relocates an item beneath a
    separate ``_long-paths`` folder. The path stored here is only the browseable
    backup payload path; the catalogue snapshot retains the real virtual names.
    """
    parts=[str(x) for x in rel_parts if str(x)]
    candidate='bookmarks/'+('/'.join(parts))
    if len(candidate)<=MAX_FOLDER_BACKUP_REL:return parts
    if not parts:return parts
    current=parts[-1]
    if kind=='bookmark':
        suffix=ext or Path(current).suffix
        stem=Path(current).stem[:56].rstrip(' .-_') or 'File'
        current=f'{stem} [{int(node_id)}]{suffix}'
    else:
        stem=current[:56].rstrip(' .-_') or 'Folder'
        current=f'{stem} [{int(node_id)}]'
    parts[-1]=current
    # If the parent chain itself is already exceptionally long, keep this
    # component minimal. Earlier folder levels will already have been shortened
    # as they were planned, so this is a defensive final bound rather than a
    # separate fallback hierarchy.
    candidate='bookmarks/'+('/'.join(parts))
    if len(candidate)>MAX_FOLDER_BACKUP_REL:
        parts[-1]=(f'{int(node_id)}{ext}' if kind=='bookmark' else f'Folder [{int(node_id)}]')
    return parts

def _flat_backup_plan(conn):
    """Build a real-folder backup view of the virtual SQLite hierarchy."""
    rows=conn.execute(
        "SELECT n.*,f.storage_name FROM nodes n LEFT JOIN files f ON f.id=n.file_id "
        "ORDER BY n.parent_id,n.position,n.id"
    ).fetchall()
    by_parent={}
    for r in rows:by_parent.setdefault(r['parent_id'],[]).append(r)
    root_id=_db_root_id(conn);entries=[]

    def unique_component(base,used,suffix):
        candidate=base
        if candidate.casefold() not in used:
            used.add(candidate.casefold());return candidate
        stem=Path(base).stem;ext=Path(base).suffix
        candidate=f'{stem} ({suffix}){ext}';n=2
        while candidate.casefold() in used:
            candidate=f'{stem} ({suffix}-{n}){ext}';n+=1
        used.add(candidate.casefold());return candidate

    def walk(parent_id,rel_parts):
        used=set()
        for r in by_parent.get(parent_id,[]):
            nid=int(r['id']);kind=str(r['kind']);display=str(r['display_name'] or '').strip()
            if kind=='folder':
                comp=unique_component(_backup_safe_component(display,'Untitled folder'),used,nid)
                rel=_folder_backup_rel(rel_parts+[comp],nid,'folder')
                entries.append({'nodeId':nid,'kind':'folder','path':'/'.join(rel),'displayName':display})
                walk(nid,rel)
            elif kind=='bookmark':
                storage=str(r['storage_name'] or r['physical_name'] or '')
                ext=Path(storage).suffix
                title=display or Path(storage).stem or 'Untitled'
                name=_backup_safe_component(title,'Untitled')
                if ext and Path(name).suffix.lower()!=ext.lower():name+=ext
                name=unique_component(name,used,nid)
                rel=_folder_backup_rel(rel_parts+[name],nid,'bookmark',ext)
                entries.append({'nodeId':nid,'kind':'bookmark','path':'/'.join(rel),'displayName':display,'storageName':storage,'fileId':r['file_id']})
    walk(root_id,[])
    return entries

def _backup_staging_dir():
    """Local scratch space for small temporary backup artefacts such as DB snapshots."""
    p=STASHLIBRARY_APP_ROOT/'BackupStaging';p.mkdir(parents=True,exist_ok=True);return p

def _set_hidden_windows(path):
    """Best-effort hide an internal work directory on Windows."""
    if os.name!='nt':return
    try:
        import ctypes
        FILE_ATTRIBUTE_HIDDEN=0x2
        INVALID_FILE_ATTRIBUTES=0xFFFFFFFF
        kernel32=ctypes.windll.kernel32
        current=kernel32.GetFileAttributesW(str(Path(path)))
        if current!=INVALID_FILE_ATTRIBUTES:
            kernel32.SetFileAttributesW(str(Path(path)),current|FILE_ATTRIBUTE_HIDDEN)
    except Exception:pass


def _clear_hidden_windows(path):
    """Best-effort make a completed backup visible again on Windows."""
    if os.name!='nt':return
    try:
        import ctypes
        FILE_ATTRIBUTE_HIDDEN=0x2
        INVALID_FILE_ATTRIBUTES=0xFFFFFFFF
        kernel32=ctypes.windll.kernel32
        current=kernel32.GetFileAttributesW(str(Path(path)))
        if current!=INVALID_FILE_ATTRIBUTES:
            kernel32.SetFileAttributesW(str(Path(path)),current & ~FILE_ATTRIBUTE_HIDDEN)
    except Exception:pass

def _backup_work_root(backups):
    """Hidden in-destination work area so users only see completed backups."""
    p=Path(backups)/'.stashlibrary-backup-work';p.mkdir(parents=True,exist_ok=True);_set_hidden_windows(p);return p

def _cleanup_stale_backup_partials(backups=None,older_than_seconds=24*60*60):
    """Clean old interrupted backup work without exposing partial backups.

    v0.10.150 writes directly into the destination folder and uses a hidden
    marker to distinguish incomplete work. Older .working/.partial layouts are
    still cleaned for backwards compatibility.
    """
    now=time.time();removed=0
    roots=[_backup_staging_dir()]
    if backups:
        roots.extend([Path(backups),Path(backups)/'.stashlibrary-backup-work'])
    seen=set()
    for root in roots:
        try:key=os.path.normcase(str(root.resolve(strict=False)))
        except Exception:key=str(root)
        if key in seen:continue
        seen.add(key)
        if not root.exists():continue
        patterns=(
            'StashLibrary-backup-*.partial','StashLibrary-backup-*.working','StashLibrary-backup-*-*.working',
            'StashLibrary-backup-*.partial','StashLibrary-backup-*.working','StashLibrary-backup-*-*.working',
        )
        for pattern in patterns:
            for p in root.glob(pattern):
                try:
                    if now-p.stat().st_mtime<older_than_seconds:continue
                    if p.is_dir():shutil.rmtree(p)
                    elif p.is_file():p.unlink()
                    removed+=1
                except Exception:pass
    if backups:
        # New folder backups are hidden while this marker exists. An abrupt PC
        # shutdown can leave one behind; remove only stale incomplete backups.
        try:
            for pattern in ('StashLibrary-backup-*','StashLibrary-backup-*'):
                for p in Path(backups).glob(pattern):
                    marker=p/BACKUP_INCOMPLETE_MARKER if p.is_dir() else None
                    if not marker or not marker.exists():continue
                    try:
                        age=now-max(p.stat().st_mtime,marker.stat().st_mtime)
                        if age<older_than_seconds:continue
                        shutil.rmtree(p);removed+=1
                    except Exception:pass
        except Exception:pass
    return removed

def _sha256_file(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for chunk in iter(lambda:f.read(8*1024*1024),b''):h.update(chunk)
    return h.hexdigest()

def _copy_file_with_hash(source,target,progress_cb=None):
    """Copy one file and calculate its SHA-256 during the same read pass."""
    _check_backup_cancelled()
    source=Path(source);target=Path(target);target.parent.mkdir(parents=True,exist_ok=True)
    h=hashlib.sha256();size=0
    with source.open('rb') as src,target.open('wb') as out:
        while True:
            _check_backup_cancelled()
            chunk=src.read(8*1024*1024)
            if not chunk:break
            out.write(chunk);h.update(chunk);size+=len(chunk)
            if progress_cb:progress_cb(len(chunk))
        out.flush()
    try:shutil.copystat(source,target)
    except Exception:pass
    if not target.is_file() or target.stat().st_size!=size:
        raise RuntimeError(f'Backup copy failed: size mismatch for {source.name}.')
    return {'size':size,'sha256':h.hexdigest()}

def _write_backup_integrity(root,started=None,percent_start=86,percent_end=90):
    """Compatibility helper used outside the normal creation path."""
    root=Path(root);integrity=root/'stashlibrary-backup-integrity.json'
    files=[p for p in root.rglob('*') if p.is_file() and p!=integrity]
    total=max(len(files),1);records={}
    for idx,p in enumerate(files,1):
        rel=p.relative_to(root).as_posix()
        records[rel]={'size':p.stat().st_size,'sha256':_sha256_file(p)}
        progress('Backup','hashing backup contents',rel,float(percent_start)+(float(percent_end)-float(percent_start))*(idx/total),started)
    integrity.write_text(json.dumps({'format':'StashLibrary backup integrity','version':1,'files':records},indent=2,ensure_ascii=False),encoding='utf-8')
    return records

def _verify_backup_folder(root,flat_layout=None,started=None,percent_start=90,percent_end=96,verify_hashes=True,require_integrity=True,allow_incomplete_marker=False):
    """Verify a browseable folder backup.

    Creation uses structural/existence/size verification only because SHA-256 is
    calculated inline while each file is copied. Import/restore performs the
    full checksum pass.
    """
    root=Path(root)
    if not root.is_dir():raise RuntimeError('Backup verification failed: backup folder is missing.')
    incomplete_marker=root/BACKUP_INCOMPLETE_MARKER
    if incomplete_marker.exists() and not allow_incomplete_marker:
        raise RuntimeError('Backup verification failed: this backup was not completed.')
    info_path=root/'backup-info.json'
    if not info_path.is_file():raise RuntimeError('Backup verification failed: backup-info.json is missing.')
    try:info=json.loads(info_path.read_text(encoding='utf-8'))
    except Exception as e:raise RuntimeError(f'Backup verification failed: backup metadata cannot be read: {e}')
    if str(info.get('format') or '')!='StashLibrary backup':raise RuntimeError('Backup verification failed: backup metadata is not recognised.')
    if flat_layout is None:flat_layout=(root/'stashlibrary-backup-manifest.json').is_file()
    manifest=None
    if flat_layout:
        manifest_path=root/'stashlibrary-backup-manifest.json';db=root/'stashlibrary-system'/'stashlibrary.sqlite3'
        if not manifest_path.is_file() or not db.is_file():raise RuntimeError('Backup verification failed: catalogue manifest or SQLite snapshot is missing.')
        try:
            manifest=json.loads(manifest_path.read_text(encoding='utf-8'))
            if int(manifest.get('version') or 0)<2:raise RuntimeError('catalogue manifest version is invalid')
            with sqlite3.connect(f'file:{db.as_posix()}?mode=ro&immutable=1',uri=True) as check:
                row=check.execute('PRAGMA quick_check').fetchone()
                if not row or str(row[0]).lower()!='ok':raise RuntimeError('SQLite catalogue quick_check failed')
        except RuntimeError:raise
        except Exception as e:raise RuntimeError(f'Backup verification failed: {e}')

        # The catalogue manifest is the authoritative list of archive payloads.
        # Never allow a backup to be declared complete if even one expected
        # bookmark file was silently skipped.
        missing=[];unsafe=[]
        for e in manifest.get('entries',[]):
            if e.get('kind')!='bookmark':continue
            rel=str(e.get('path') or '')
            rp=Path(rel)
            if not rel or rp.is_absolute() or '..' in rp.parts:
                unsafe.append(rel or '<empty>');continue
            if not (root/'bookmarks'/rp).is_file():missing.append(rel)
        if unsafe:raise RuntimeError(f'Backup verification failed: unsafe bookmark path in manifest: {unsafe[0]}')
        if missing:
            preview=', '.join(missing[:3]);more=f' (+{len(missing)-3} more)' if len(missing)>3 else ''
            raise RuntimeError(f'Backup verification failed: {len(missing)} archived file(s) are missing from the backup: {preview}{more}')

    integrity_path=root/'stashlibrary-backup-integrity.json'
    if not integrity_path.is_file():
        if require_integrity:raise RuntimeError('Backup verification failed: integrity manifest is missing.')
        return True
    try:integrity=json.loads(integrity_path.read_text(encoding='utf-8'))
    except Exception as e:raise RuntimeError(f'Backup verification failed: integrity manifest cannot be read: {e}')
    records=integrity.get('files') or {}
    if not isinstance(records,dict):raise RuntimeError('Backup verification failed: integrity manifest is invalid.')

    # The integrity manifest must describe every payload/metadata file exactly.
    # This is a cheap directory/stat inventory check and catches a truncated copy
    # without rereading gigabytes of data.
    actual={p.relative_to(root).as_posix() for p in root.rglob('*') if p.is_file() and p!=integrity_path and p!=incomplete_marker}
    expected=set(str(k) for k in records.keys())
    missing_records=sorted(expected-actual);unrecorded=sorted(actual-expected)
    if missing_records:
        raise RuntimeError(f'Backup verification failed: expected file is missing: {missing_records[0]}')
    if unrecorded:
        raise RuntimeError(f'Backup verification failed: file inventory is incomplete: {unrecorded[0]} is not recorded.')

    total=max(len(records),1)
    for idx,(rel,expected_meta) in enumerate(records.items(),1):
        rel_path=Path(str(rel))
        if rel_path.is_absolute() or '..' in rel_path.parts:raise RuntimeError('Backup verification failed: unsafe integrity path.')
        p=root/rel_path
        if not p.is_file():raise RuntimeError(f'Backup verification failed: missing file {rel}.')
        if int(expected_meta.get('size',-1))!=p.stat().st_size:raise RuntimeError(f'Backup verification failed: size mismatch for {rel}.')
        if verify_hashes and str(expected_meta.get('sha256') or '').lower()!=_sha256_file(p).lower():raise RuntimeError(f'Backup verification failed: checksum mismatch for {rel}.')
        progress('Backup','verifying backup folder' if verify_hashes else 'checking backup folder',rel,float(percent_start)+(float(percent_end)-float(percent_start))*(idx/total),started)
    return True

def _copy_backup_folder(source,dest,started=None,percent_start=96,percent_end=99):
    source=Path(source);dest=Path(dest)
    if dest.exists():shutil.rmtree(dest)
    dest.mkdir(parents=True,exist_ok=False)
    dirs=[p for p in source.rglob('*') if p.is_dir()]
    for d in dirs:(dest/d.relative_to(source)).mkdir(parents=True,exist_ok=True)
    files=[p for p in source.rglob('*') if p.is_file()]
    total=max(len(files),1)
    for idx,p in enumerate(files,1):
        rel=p.relative_to(source);target=dest/rel;target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(p,target)
        progress('Backup','copying verified backup folder',rel.as_posix(),float(percent_start)+(float(percent_end)-float(percent_start))*(idx/total),started)
    return dest


# v0.10.158 returns to compressed ZIP backups.  The path plan intentionally
# preserves StashLibrary's natural hierarchy rather than relocating long members into
# a special _long-paths directory.  7-Zip handles those long member names more
# reliably than Windows Explorer's built-in ZIP shell.
def _flat_zip_backup_plan(conn):
    rows=conn.execute(
        "SELECT n.*,f.storage_name FROM nodes n LEFT JOIN files f ON f.id=n.file_id "
        "ORDER BY n.parent_id,n.position,n.id"
    ).fetchall()
    by_parent={}
    for r in rows:by_parent.setdefault(r['parent_id'],[]).append(r)
    root_id=_db_root_id(conn);entries=[]

    def unique_component(base,used,suffix):
        candidate=base
        if candidate.casefold() not in used:
            used.add(candidate.casefold());return candidate
        stem=Path(base).stem;ext=Path(base).suffix
        candidate=f'{stem} ({suffix}){ext}';n=2
        while candidate.casefold() in used:
            candidate=f'{stem} ({suffix}-{n}){ext}';n+=1
        used.add(candidate.casefold());return candidate

    def walk(parent_id,rel_parts):
        used=set()
        for r in by_parent.get(parent_id,[]):
            nid=int(r['id']);kind=str(r['kind']);display=str(r['display_name'] or '').strip()
            if kind=='folder':
                comp=unique_component(_backup_safe_component(display,'Untitled folder'),used,nid)
                rel=rel_parts+[comp]
                entries.append({'nodeId':nid,'kind':'folder','path':'/'.join(rel),'displayName':display})
                walk(nid,rel)
            elif kind=='bookmark':
                storage=str(r['storage_name'] or r['physical_name'] or '')
                ext=Path(storage).suffix
                title=display or Path(storage).stem or 'Untitled'
                name=_backup_safe_component(title,'Untitled')
                if ext and Path(name).suffix.lower()!=ext.lower():name+=ext
                name=unique_component(name,used,nid)
                rel=rel_parts+[name]
                entries.append({'nodeId':nid,'kind':'bookmark','path':'/'.join(rel),'displayName':display,'storageName':storage,'fileId':r['file_id']})
    walk(root_id,[])
    return entries


def _zip_write_file(z,source,arcname,progress_cb=None):
    """Write one ZIP member in chunks so cancellation stays responsive."""
    _check_backup_cancelled();source=Path(source)
    with source.open('rb') as src,z.open(str(arcname).replace('\\','/'),'w',force_zip64=True) as out:
        while True:
            _check_backup_cancelled()
            chunk=src.read(4*1024*1024)
            if not chunk:break
            out.write(chunk)
            if progress_cb:progress_cb(len(chunk))


def _verify_backup_zip(path,started=None,percent_start=84,percent_end=92,allow_cancel=True):
    """Read every ZIP member to EOF so Python validates its CRC and inventory."""
    path=Path(path)
    if not path.is_file() or not zipfile.is_zipfile(path):
        raise RuntimeError('Backup verification failed: the completed file is not a valid ZIP archive.')
    try:
        with zipfile.ZipFile(path,'r') as z:
            infos=z.infolist();names={x.filename for x in infos}
            if any(not _safe_zip_member(n) for n in names):raise RuntimeError('Backup verification failed: unsafe path found inside ZIP archive.')
            if 'backup-info.json' not in names:raise RuntimeError('Backup verification failed: backup-info.json is missing.')
            info=json.loads(z.read('backup-info.json').decode('utf-8'))
            if str(info.get('format') or '')!='StashLibrary backup':raise RuntimeError('Backup verification failed: backup metadata is not recognised.')
            manifest=None
            if 'stashlibrary-backup-manifest.json' in names:
                manifest=json.loads(z.read('stashlibrary-backup-manifest.json').decode('utf-8'))
                if int(manifest.get('version') or 0)<2:raise RuntimeError('Backup verification failed: catalogue manifest version is invalid.')
                required={'stashlibrary-system/stashlibrary.sqlite3'}
                for e in manifest.get('entries',[]) or []:
                    if e.get('kind')=='bookmark':
                        rel=str(e.get('path') or '').replace('\\','/')
                        if not rel:raise RuntimeError('Backup verification failed: bookmark path is missing from the manifest.')
                        required.add('bookmarks/'+rel)
                for name in manifest.get('untrackedFiles',[]) or []:
                    required.add('stashlibrary-system/untracked-files/'+Path(str(name)).name)
                missing=sorted(required-names)
                if missing:
                    preview=', '.join(missing[:3]);more=f' (+{len(missing)-3} more)' if len(missing)>3 else ''
                    raise RuntimeError(f'Backup verification failed: {len(missing)} expected file(s) are missing: {preview}{more}')
            members=[x for x in infos if not x.is_dir()];total_bytes=max(sum(max(0,int(x.file_size)) for x in members),1);done=0
            span=max(0,float(percent_end)-float(percent_start))
            for member in members:
                if allow_cancel:_check_backup_cancelled()
                with z.open(member,'r') as f:
                    while True:
                        if allow_cancel:_check_backup_cancelled()
                        chunk=f.read(4*1024*1024)
                        if not chunk:break
                        done+=len(chunk)
                        progress('Backup','verifying ZIP contents',member.filename,float(percent_start)+span*(done/total_bytes),started)
    except (RuntimeError,BackupCancelled):raise
    except Exception as e:raise RuntimeError(f'Backup verification failed: {e}')
    return True


def _copy_zip_to_partial(source,partial,started=None,percent_start=92,percent_end=97):
    source=Path(source);partial=Path(partial);partial.parent.mkdir(parents=True,exist_ok=True)
    total=max(source.stat().st_size,1);done=0;h=hashlib.sha256()
    with source.open('rb') as src,partial.open('wb') as out:
        while True:
            _check_backup_cancelled()
            chunk=src.read(8*1024*1024)
            if not chunk:break
            out.write(chunk);h.update(chunk);done+=len(chunk)
            progress('Backup','copying verified ZIP',partial.name,float(percent_start)+(float(percent_end)-float(percent_start))*(done/total),started)
        out.flush()
        try:os.fsync(out.fileno())
        except Exception:pass
    _set_hidden_windows(partial)
    return h.hexdigest()


def _sha256_zip(path,started=None,percent_start=97,percent_end=99.2):
    path=Path(path);total=max(path.stat().st_size,1);done=0;h=hashlib.sha256()
    with path.open('rb') as f:
        while True:
            _check_backup_cancelled()
            chunk=f.read(8*1024*1024)
            if not chunk:break
            h.update(chunk);done+=len(chunk)
            progress('Backup','checking copied ZIP',path.name,float(percent_start)+(float(percent_end)-float(percent_start))*(done/total),started)
    return h.hexdigest()


def _publish_zip_with_retry(partial,dest):
    partial=Path(partial);dest=Path(dest);last=None
    for attempt in range(9):
        _check_backup_cancelled()
        try:os.replace(partial,dest);_clear_hidden_windows(dest);return
        except Exception as e:
            last=e
            if attempt<8:time.sleep(0.12*(attempt+1))
    raise RuntimeError(f'Could not publish the completed backup ZIP: {last}')

def create_backup(reason='manual',destination=None):
    bookmarks=bookmarks_path();configured_backups=backups_path()
    if not bookmarks:raise RuntimeError('Choose a StashLibrary folder first.')
    stamp=datetime.now().strftime('%Y-%m-%d_%H%M%S')
    if destination:
        dest=Path(destination)
        if dest.suffix.lower()!='.zip':dest=dest.with_suffix('.zip')
        backups=dest.parent
    else:
        backups=configured_backups
        if not backups:raise RuntimeError('Choose a backup folder first.')
        dest=backups/f'StashLibrary-backup-{stamp}.zip';i=2
        while dest.exists() or (backups/(dest.name+'.partial')).exists():
            dest=backups/f'StashLibrary-backup-{stamp}-{i}.zip';i+=1
    bookmarks.mkdir(parents=True,exist_ok=True);backups.mkdir(parents=True,exist_ok=True);started=time.monotonic()
    flat_layout=is_flat_layout();staging=_backup_staging_dir();_cleanup_stale_backup_partials(backups)
    partial=backups/(dest.name+'.partial')
    working=staging/f'{dest.stem}-{uuid.uuid4().hex}.working'
    db_snapshot=None
    if not BACKUP_RUN_LOCK.acquire(blocking=False):raise RuntimeError('A StashLibrary backup is already in progress.')
    BACKUP_CANCEL_EVENT.clear();progress('Backup','building compressed ZIP locally',dest.name,0,started)
    try:
        _check_backup_cancelled();backup_entries=[];untracked=[]
        if flat_layout:
            db_snapshot=staging/f'stashlibrary-catalogue-{uuid.uuid4().hex}.sqlite3'
            with DB_LOCK:
                conn=_db_connect()
                try:
                    conn.commit();conn.execute('PRAGMA wal_checkpoint(FULL)');conn.commit();backup_entries=_flat_zip_backup_plan(conn)
                    with sqlite3.connect(str(db_snapshot)) as snapshot_conn:conn.backup(snapshot_conn);snapshot_conn.commit()
                finally:conn.close()
            with sqlite3.connect(f'file:{Path(db_snapshot).as_posix()}?mode=ro&immutable=1',uri=True) as check:
                row=check.execute('PRAGMA quick_check').fetchone()
                if not row or str(row[0]).lower()!='ok':raise RuntimeError('Could not verify the SQLite catalogue snapshot before backup.')
            arc=flat_archive_dir();file_entries=[e for e in backup_entries if e.get('kind')=='bookmark']
            missing=[]
            for e in file_entries:
                storage=str(e.get('storageName') or '')
                if not storage or not (arc/storage).is_file():missing.append(e.get('displayName') or storage or f"item {e.get('nodeId')}")
            if missing:
                preview=', '.join(str(x) for x in missing[:3]);more=f' (+{len(missing)-3} more)' if len(missing)>3 else ''
                raise RuntimeError(f'Backup not created: {len(missing)} StashLibrary archive file(s) are missing from live storage: {preview}{more}. Run Rescan & Resync Library before trying again.')
            tracked={str(e.get('storageName') or '') for e in file_entries if str(e.get('storageName') or '')}
            try:
                for p in arc.iterdir():
                    if not p.is_file() or p.name in tracked or p.name.startswith('.local-bookmarks-'):continue
                    if destination:
                        try:
                            if os.path.normcase(str(p.resolve(strict=False)))==os.path.normcase(str(dest.resolve(strict=False))):continue
                        except Exception:pass
                    untracked.append(p)
            except Exception:pass
            manifest={'format':'StashLibrary backup','version':5,'backupType':'zip','layout':'virtual-folders-with-flat-restore','created':datetime.now().isoformat(timespec='seconds'),'reason':reason,'entries':backup_entries,'untrackedFiles':[p.name for p in untracked]}
            info={k:v for k,v in manifest.items() if k not in {'entries','untrackedFiles'}}
            info['expectedBookmarks']=len(file_entries);info['preservedUntrackedFiles']=len(untracked)
            recovery=flat_recovery_file();sources=[]
            sources.append((Path(db_snapshot),'stashlibrary-system/stashlibrary.sqlite3','SQLite catalogue'))
            if recovery and recovery.is_file():sources.append((recovery,'stashlibrary-system/catalogue-recovery.json','Catalogue recovery data'))
            for e in file_entries:
                src=arc/str(e.get('storageName') or '');sources.append((src,'bookmarks/'+str(e['path']).replace('\\','/'),e.get('displayName') or src.name))
            for p in untracked:sources.append((p,'stashlibrary-system/untracked-files/'+p.name,f'Untracked: {p.name}'))
            total_bytes=max(sum(max(0,s.stat().st_size) for s,_,_ in sources),1);done_bytes=0
            def bump(n,label=''):
                nonlocal done_bytes
                _check_backup_cancelled();done_bytes+=n
                progress('Backup','compressing files',label or dest.name,5+77*(done_bytes/total_bytes),started)
            with zipfile.ZipFile(working,'w',compression=zipfile.ZIP_DEFLATED,compresslevel=6,allowZip64=True) as z:
                z.writestr('backup-info.json',json.dumps(info,indent=2,ensure_ascii=False))
                z.writestr('stashlibrary-backup-manifest.json',json.dumps(manifest,indent=2,ensure_ascii=False))
                for e in backup_entries:
                    if e.get('kind')=='folder':z.writestr('bookmarks/'+str(e['path']).rstrip('/')+'/',b'')
                if not backup_entries:z.writestr('bookmarks/',b'')
                for src,arcname,label in sources:_zip_write_file(z,src,arcname,lambda n,l=label:bump(n,l))
        else:
            info={'format':'StashLibrary backup','version':5,'backupType':'zip','layout':'physical-folders','created':datetime.now().isoformat(timespec='seconds'),'reason':reason}
            backup_resolved=None
            if not destination:
                try:backup_resolved=backups.resolve(strict=False)
                except Exception:backup_resolved=backups
            files=[]
            for q in bookmarks.rglob('*'):
                if not q.is_file() or q.name.startswith('.local-bookmarks-'):continue
                if backup_resolved is not None:
                    try:q.resolve(strict=False).relative_to(backup_resolved);continue
                    except Exception:pass
                if destination and os.path.normcase(str(q.resolve(strict=False)))==os.path.normcase(str(dest.resolve(strict=False))):continue
                files.append(q)
            total_bytes=max(sum(max(0,q.stat().st_size) for q in files),1);done_bytes=0
            def bump_physical(n,label=''):
                nonlocal done_bytes
                _check_backup_cancelled();done_bytes+=n;progress('Backup','compressing files',label or dest.name,5+77*(done_bytes/total_bytes),started)
            with zipfile.ZipFile(working,'w',compression=zipfile.ZIP_DEFLATED,compresslevel=6,allowZip64=True) as z:
                z.writestr('backup-info.json',json.dumps(info,indent=2,ensure_ascii=False))
                if not files:z.writestr('bookmarks/',b'')
                for q in files:_zip_write_file(z,q,(Path('bookmarks')/q.relative_to(bookmarks)).as_posix(),lambda n,l=q.name:bump_physical(n,l))

        _check_backup_cancelled();progress('Backup','verifying local ZIP',dest.name,83,started)
        _verify_backup_zip(working,started,84,92,allow_cancel=True)
        _check_backup_cancelled()
        try:
            if partial.exists():partial.unlink()
        except Exception:pass
        source_hash=_copy_zip_to_partial(working,partial,started,92,97)
        if partial.stat().st_size!=working.stat().st_size:raise RuntimeError('Backup copy verification failed: destination size does not match.')
        copied_hash=_sha256_zip(partial,started,97,99.2)
        if copied_hash!=source_hash:raise RuntimeError('Backup copy verification failed: destination checksum does not match.')
        if not zipfile.is_zipfile(partial):raise RuntimeError('Backup copy verification failed: destination is not a readable ZIP archive.')
        progress('Backup','publishing verified ZIP',dest.name,99.6,started);_publish_zip_with_retry(partial,dest)
        progress('Backup','finished',dest.name,100,started);return dest
    except BackupCancelled:
        progress('Backup','cancelled','Backup creation cancelled',None,started);raise
    except Exception as e:
        progress('Backup','failed',str(e),None,started);raise
    finally:
        for q in (working,db_snapshot,partial):
            if not q:continue
            try:
                if Path(q).exists():Path(q).unlink()
            except Exception:pass
        BACKUP_CANCEL_EVENT.clear()
        if BACKUP_RUN_LOCK.locked():BACKUP_RUN_LOCK.release()

def create_manual_backup_interactive():
    stamp=datetime.now().strftime('%Y-%m-%d_%H%M%S')
    initial=Path.home()/'Documents'
    if not initial.is_dir():initial=Path.home()
    try:
        chosen=_win_file_dialog(
            title='Save StashLibrary Manual Backup',save=True,
            initialfile=f'StashLibrary-backup-{stamp}.zip',initialdir=str(initial),defaultext='zip'
        )
    except Exception as e:raise RuntimeError(f'Manual backup picker failed: {e}')
    if not chosen:return {'cancelled':True,'path':''}
    dest=Path(chosen)
    if dest.suffix.lower()!='.zip':dest=dest.with_suffix('.zip')
    created=create_backup('manual',destination=dest)
    return {'cancelled':False,'path':str(created)}


def latest_backup():
    items=_finished_backup_items();return items[0] if items else None


def export_backup():
    created=create_backup('export')
    try:
        out=_win_file_dialog(title='Export StashLibrary backup ZIP',save=True,initialfile=created.name,initialdir=str(created.parent),defaultext='zip')
        if not out:return {'cancelled':True,'backup':str(created)}
        out=Path(out)
        if os.path.normcase(str(out.resolve(strict=False)))==os.path.normcase(str(created.resolve(strict=False))):return {'path':str(created),'backup':str(created)}
        shutil.copy2(created,out);_verify_backup_zip(out,None,0,0,allow_cancel=False)
        return {'path':str(out),'backup':str(created)}
    except Exception as e:raise RuntimeError(f'Export picker failed: {e}')

def _safe_zip_member(name):
    p=Path(name);return not p.is_absolute() and '..' not in p.parts

def _restore_backup_tree(temp,source_label):
    temp=Path(temp);bookmarks=bookmarks_path();started=time.monotonic();progress('Import','validating backup',source_label,0,started)
    if not (temp/'backup-info.json').is_file():raise RuntimeError('The selected folder is not a StashLibrary backup (backup-info.json is missing).')
    # New folder backups carry integrity metadata. Legacy extracted ZIPs may not.
    if (temp/'stashlibrary-backup-integrity.json').is_file():_verify_backup_folder(temp,None,started,1,15)
    manifest_path=temp/'stashlibrary-backup-manifest.json'
    if manifest_path.is_file():
        manifest=json.loads(manifest_path.read_text(encoding='utf-8'))
        if int(manifest.get('version') or 0)>=2 and manifest.get('layout')=='virtual-folders-with-flat-restore':
            progress('Import','restoring backed-up catalogue',source_label,20,started)
            arc=flat_archive_dir();arc.mkdir(parents=True,exist_ok=True)
            if uses_separated_internal_data():
                try:
                    def owned_names(conn):return [str(r['storage_name']) for r in conn.execute('SELECT storage_name FROM files').fetchall() if str(r['storage_name'] or '')]
                    old_names=_with_db(owned_names)
                except Exception:old_names=[]
                for name in old_names:
                    try:
                        target=arc/Path(name).name
                        if target.is_file():target.unlink()
                    except Exception:pass
                data=internal_data_dir(create=True)
                for suffix in ('','-wal','-shm'):
                    q=data/(DATABASE_NAME+suffix)
                    try:
                        if q.exists():q.unlink()
                    except Exception:pass
                for q in (data/FLAT_RECOVERY_NAME,data/'history.json'):
                    try:
                        if q.exists():q.unlink()
                    except Exception:pass
                shutil.rmtree(data/'undo-store',ignore_errors=True)
            else:
                sysdir=bookmarks/SYSTEM_DIR_NAME
                if sysdir.exists():shutil.rmtree(sysdir)
                sysdir.mkdir(parents=True,exist_ok=True);arc=sysdir/FLAT_ARCHIVE_DIR_NAME;arc.mkdir(parents=True,exist_ok=True)
            db_src=temp/'stashlibrary-system'/'stashlibrary.sqlite3'
            if not db_src.is_file():raise RuntimeError('This StashLibrary backup is missing its SQLite catalogue snapshot.')
            db_target=database_file(bookmarks);db_target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(db_src,db_target)
            rec_src=temp/'stashlibrary-system'/'catalogue-recovery.json'
            if rec_src.is_file():shutil.copy2(rec_src,flat_recovery_file())
            entries=[e for e in manifest.get('entries',[]) if e.get('kind')=='bookmark'];total=max(len(entries),1)
            for idx,e in enumerate(entries,1):
                rel=str(e.get('path') or '');storage=str(e.get('storageName') or '')
                if not rel or not storage:raise RuntimeError('Backup manifest contains an incomplete bookmark entry.')
                payload=temp/'bookmarks'/Path(rel)
                if not payload.is_file():raise RuntimeError(f'Backup is missing archived file: {rel}')
                target=arc/storage;target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(payload,target)
                progress('Import','restoring archive files',e.get('displayName') or storage,20+(idx/total*72),started)
            # Files found in the live archive but not represented by the catalogue
            # are deliberately preserved by v0.10.149 backups. Restore them too
            # rather than silently losing those recovery copies.
            untracked_dir=temp/'stashlibrary-system'/'untracked-files'
            for name in manifest.get('untrackedFiles',[]) or []:
                src=untracked_dir/Path(str(name)).name
                if not src.is_file():raise RuntimeError(f'Backup is missing preserved untracked file: {name}')
                target=arc/Path(str(name)).name
                if target.exists():
                    stem=target.stem;suffix=target.suffix;n=2
                    while target.exists():
                        target=arc/f'{stem} (recovered {n}){suffix}';n+=1
                shutil.copy2(src,target)
            with DB_LOCK:
                conn=_db_connect()
                try:conn.commit();conn.execute('PRAGMA wal_checkpoint(FULL)');conn.commit()
                finally:conn.close()
            progress('Import','finished',f'Restored {source_label}',100,started);return {'layout':'flattened'}
    # Backwards-compatible physical-folder restore.
    source=temp/'bookmarks'
    if not source.exists():raise RuntimeError('This backup does not contain a bookmarks folder.')
    progress('Import','restoring bookmarks',source_label,50,started)
    for p in list(bookmarks.iterdir()):
        if p.is_dir():shutil.rmtree(p)
        else:p.unlink()
    for p in source.iterdir():
        target=bookmarks/p.name
        if p.is_dir():shutil.copytree(p,target)
        else:shutil.copy2(p,target)
    progress('Import','finished',f'Restored {source_label}',100,started);return {'layout':'physical'}

def _restore_backup_zip(src):
    """Restore a ZIP after fully verifying it, without extracting long hierarchy paths.

    Flat-layout bookmark payloads are streamed to a short staging directory by
    storage name, so a ZIP that needs 7-Zip for manual browsing can still be
    restored by StashLibrary on Windows without hitting Explorer/MAX_PATH limits.
    """
    src=Path(src);started=time.monotonic();progress('Import','verifying backup ZIP',src.name,0,started)
    _verify_backup_zip(src,started,1,18,allow_cancel=False)
    stage=APPDIR/f'import-temp-{uuid.uuid4().hex}';stage.mkdir(parents=True,exist_ok=False)
    try:
        with zipfile.ZipFile(src,'r') as z:
            names=set(z.namelist())
            manifest=None
            if 'stashlibrary-backup-manifest.json' in names:
                manifest=json.loads(z.read('stashlibrary-backup-manifest.json').decode('utf-8'))
            if manifest and int(manifest.get('version') or 0)>=2 and manifest.get('layout')=='virtual-folders-with-flat-restore':
                staged_db=stage/'stashlibrary.sqlite3'
                with z.open('stashlibrary-system/stashlibrary.sqlite3','r') as inp,staged_db.open('wb') as out:shutil.copyfileobj(inp,out,4*1024*1024)
                with sqlite3.connect(f'file:{staged_db.as_posix()}?mode=ro&immutable=1',uri=True) as check:
                    row=check.execute('PRAGMA quick_check').fetchone()
                    if not row or str(row[0]).lower()!='ok':raise RuntimeError('Backup restore stopped: the SQLite snapshot failed quick_check.')
                staged_arc=stage/'archive';staged_arc.mkdir(parents=True,exist_ok=True)
                entries=[e for e in manifest.get('entries',[]) or [] if e.get('kind')=='bookmark'];total=max(len(entries),1)
                for idx,e in enumerate(entries,1):
                    rel=str(e.get('path') or '').replace('\\','/');storage=Path(str(e.get('storageName') or '')).name
                    if not rel or not storage:raise RuntimeError('Backup manifest contains an incomplete bookmark entry.')
                    member='bookmarks/'+rel
                    if member not in names:raise RuntimeError(f'Backup is missing archived file: {rel}')
                    target=staged_arc/storage
                    with z.open(member,'r') as inp,target.open('wb') as out:shutil.copyfileobj(inp,out,4*1024*1024)
                    progress('Import','staging archived files',e.get('displayName') or storage,18+(idx/total*50),started)
                staged_untracked=stage/'untracked';staged_untracked.mkdir(exist_ok=True)
                for name in manifest.get('untrackedFiles',[]) or []:
                    safe=Path(str(name)).name;member='stashlibrary-system/untracked-files/'+safe
                    if member not in names:raise RuntimeError(f'Backup is missing preserved untracked file: {safe}')
                    with z.open(member,'r') as inp,(staged_untracked/safe).open('wb') as out:shutil.copyfileobj(inp,out,4*1024*1024)
                staged_recovery=None
                if 'stashlibrary-system/catalogue-recovery.json' in names:
                    staged_recovery=stage/'catalogue-recovery.json'
                    with z.open('stashlibrary-system/catalogue-recovery.json','r') as inp,staged_recovery.open('wb') as out:shutil.copyfileobj(inp,out,1024*1024)

                bookmarks=bookmarks_path();arc=flat_archive_dir();arc.mkdir(parents=True,exist_ok=True)
                progress('Import','restoring verified backup',src.name,70,started)
                with DB_LOCK:
                    try:
                        def owned_names(conn):return [str(r['storage_name']) for r in conn.execute('SELECT storage_name FROM files').fetchall() if str(r['storage_name'] or '')]
                        old_names=_with_db(owned_names)
                    except Exception:old_names=[]
                    for old in old_names:
                        try:
                            q=arc/Path(old).name
                            if q.is_file():q.unlink()
                        except Exception:pass
                    data=internal_data_dir(create=True)
                    if uses_separated_internal_data():
                        for suffix in ('','-wal','-shm'):
                            q=data/(DATABASE_NAME+suffix)
                            try:
                                if q.exists():q.unlink()
                            except Exception:pass
                        for q in (data/FLAT_RECOVERY_NAME,data/'history.json'):
                            try:
                                if q.exists():q.unlink()
                            except Exception:pass
                        shutil.rmtree(data/'undo-store',ignore_errors=True)
                    else:
                        sysdir=bookmarks/SYSTEM_DIR_NAME
                        if sysdir.exists():shutil.rmtree(sysdir)
                        sysdir.mkdir(parents=True,exist_ok=True);arc=sysdir/FLAT_ARCHIVE_DIR_NAME;arc.mkdir(parents=True,exist_ok=True)
                    db_target=database_file(bookmarks);db_target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(staged_db,db_target)
                    if staged_recovery and staged_recovery.is_file():shutil.copy2(staged_recovery,flat_recovery_file())
                    staged_files=list(staged_arc.iterdir());total_files=max(len(staged_files),1)
                    for idx,q in enumerate(staged_files,1):
                        shutil.copy2(q,arc/q.name);progress('Import','restoring archive files',q.name,72+(idx/total_files*23),started)
                    for q in staged_untracked.iterdir():
                        target=arc/q.name
                        if target.exists():
                            stem=target.stem;suffix=target.suffix;n=2
                            while target.exists():target=arc/f'{stem} (recovered {n}){suffix}';n+=1
                        shutil.copy2(q,target)
                    conn=_db_connect()
                    try:conn.commit();conn.execute('PRAGMA wal_checkpoint(FULL)');conn.commit()
                    finally:conn.close()
                progress('Import','finished',f'Restored {src.name}',100,started);return {'layout':'flattened'}

            # Version-1 / physical-folder ZIP compatibility path.
            temp=stage/'physical';temp.mkdir()
            members=z.namelist()
            if any(not _safe_zip_member(n) for n in members):raise RuntimeError('Unsafe path found inside ZIP archive.')
            z.extractall(temp)
        return _restore_backup_tree(temp,src.name)
    finally:shutil.rmtree(stage,ignore_errors=True)


def import_backup():
    try:chosen=_win_file_dialog(title='Choose StashLibrary Manual Backup ZIP',save=False,defaultext='zip')
    except Exception as e:raise RuntimeError(f'Import picker failed: {e}')
    if not chosen:return {'cancelled':True}
    src=Path(chosen)
    if not zipfile.is_zipfile(src):raise RuntimeError('The selected file is not a valid ZIP archive.')
    result=_restore_backup_zip(src);return {'path':str(src),**result}


def import_webdav_backup_folder():
    """Restore a downloaded WebDAV Cloud Backup folder without connecting to WebDAV."""
    global undo_stack,redo_stack
    try:chosen=_win_choose_folder('Choose Downloaded StashLibrary Cloud Backup Folder',str(Path.home()))
    except Exception as e:raise RuntimeError(f'Cloud backup folder picker failed: {e}')
    if not chosen:return {'cancelled':True}
    selected=Path(chosen)
    root=selected
    if not (root/WEBDAV_STATE_NAME).is_file() and (root/WEBDAV_REMOTE_ROOT/WEBDAV_STATE_NAME).is_file():
        root=root/WEBDAV_REMOTE_ROOT
    state_path=root/WEBDAV_STATE_NAME;db_src=root/WEBDAV_DB_NAME;files_dir=root/WEBDAV_REMOTE_FILES
    if not state_path.is_file() or not db_src.is_file() or not files_dir.is_dir():
        raise RuntimeError('That is not a StashLibrary cloud backup folder. Choose the folder containing backup-state.json, stashlibrary.sqlite3 and the files folder.')
    try:state=json.loads(state_path.read_text(encoding='utf-8'))
    except Exception:raise RuntimeError('The selected cloud backup metadata cannot be read.')
    if state.get('format')!=WEBDAV_FORMAT:raise RuntimeError('The selected folder is not a supported StashLibrary WebDAV cloud backup.')
    files=state.get('files') if isinstance(state.get('files'),dict) else {}
    expected_db=str((state.get('database') or {}).get('sha256') or '')
    if expected_db and _webdav_sha256(db_src)!=expected_db:raise RuntimeError('The cloud backup catalogue failed its checksum check.')
    with sqlite3.connect(f'file:{db_src.as_posix()}?mode=ro&immutable=1',uri=True) as check:
        row=check.execute('PRAGMA quick_check').fetchone()
        if not row or str(row[0]).lower()!='ok':raise RuntimeError('The cloud backup catalogue failed its SQLite integrity check.')
        db_names=[str(r[0]) for r in check.execute('SELECT storage_name FROM files').fetchall() if str(r[0] or '')]
        db_nodes=int(check.execute("SELECT COUNT(*) FROM nodes WHERE kind!='root'").fetchone()[0] or 0)
        db_bookmarks=int(check.execute("SELECT COUNT(*) FROM nodes WHERE kind='bookmark'").fetchone()[0] or 0)
    state_names=set(files);db_name_set=set(db_names)
    if state_names!=db_name_set:
        raise RuntimeError('The cloud backup is inconsistent: its SQLite catalogue and files list do not describe the same archived files.')
    expected_meta=state.get('database') if isinstance(state.get('database'),dict) else {}
    for key,actual in (('nodes',db_nodes),('bookmarks',db_bookmarks),('files',len(db_names))):
        if expected_meta.get(key) is not None and int(expected_meta.get(key) or 0)!=int(actual):
            raise RuntimeError('The cloud backup catalogue table counts do not match its verified backup metadata.')
    names=list(files)
    for i,name in enumerate(names):
        if Path(name).name!=name:raise RuntimeError('The cloud backup contains an unsafe filename.')
        q=files_dir/name
        if not q.is_file():raise RuntimeError(f'The cloud backup is incomplete: {name} is missing.')
        digest=str((files.get(name) or {}).get('sha256') or '')
        if digest and _webdav_sha256(q)!=digest:raise RuntimeError(f'The cloud backup file failed its checksum check: {name}')
        progress('Cloud Restore','verifying downloaded backup',name,5+55*((i+1)/max(len(names),1)))
    missing=[name for name in db_names if not (files_dir/name).is_file()]
    if missing:raise RuntimeError(f'Cloud backup is incomplete: {len(missing)} archived file(s) referenced by the catalogue are missing.')

    root_live=bookmarks_path();arc=flat_archive_dir();live_db=database_file()
    if not root_live or not arc or not live_db:raise RuntimeError('Choose your local StashLibrary folder before restoring a backup.')
    rollback=DATA_DIR/f'folder-restore-rollback-{uuid.uuid4().hex}';rollback.mkdir(parents=True,exist_ok=True)
    old_names=[]
    try:
        with DB_LOCK:
            try:
                current=_db_connect();current.commit();current.execute('PRAGMA wal_checkpoint(FULL)');current.commit();current.close()
            except Exception:pass
            if Path(live_db).exists():shutil.copy2(live_db,rollback/WEBDAV_DB_NAME)
            try:
                with sqlite3.connect(str(live_db)) as old:
                    old_names=[str(r[0]) for r in old.execute('SELECT storage_name FROM files').fetchall() if str(r[0] or '')]
            except Exception:old_names=[]
            rbfiles=rollback/'files';rbfiles.mkdir(parents=True,exist_ok=True)
            for name in set(old_names)|set(names):
                src=arc/name
                if src.is_file():shutil.copy2(src,rbfiles/name)
            for name in old_names:
                if name not in files:
                    try:(arc/name).unlink()
                    except FileNotFoundError:pass
            for name in names:shutil.copy2(files_dir/name,arc/name)
            tmpdb=Path(live_db).with_name(f'{Path(live_db).name}.folder-restore-{uuid.uuid4().hex}.tmp');shutil.copy2(db_src,tmpdb)
            for suffix in ('-wal','-shm'):
                try:Path(str(live_db)+suffix).unlink(missing_ok=True)
                except Exception:pass
            os.replace(tmpdb,live_db)
        shutil.rmtree(rollback,ignore_errors=True)
    except Exception:
        try:
            with DB_LOCK:
                for suffix in ('-wal','-shm'):
                    try:Path(str(live_db)+suffix).unlink(missing_ok=True)
                    except Exception:pass
                if (rollback/WEBDAV_DB_NAME).is_file():shutil.copy2(rollback/WEBDAV_DB_NAME,live_db)
                rbfiles=rollback/'files'
                if rbfiles.is_dir():
                    for q in rbfiles.iterdir():
                        if q.is_file():shutil.copy2(q,arc/q.name)
        except Exception:pass
        raise
    undo_stack=[];redo_stack=[];_save_history()
    try:_with_db(lambda conn:_flat_write_recovery(conn))
    except Exception:pass
    progress('Cloud Restore','finished',root.name,100)
    return {'cancelled':False,'restored':True,'path':str(root),'files':len(files),'updatedAt':str(state.get('updatedAt') or '')}


def import_backup_folder():
    """Compatibility entry point for the temporary v0.10.147–0.10.157 folder format."""
    try:chosen=_win_choose_folder('Import Older StashLibrary Backup Folder',str(backups_path() or ''))
    except Exception as e:raise RuntimeError(f'Import picker failed: {e}')
    if not chosen:return {'cancelled':True}
    src=Path(chosen);result=_restore_backup_tree(src,src.name);return {'path':str(src),**result,'folderBackup':True}


def import_backup_zip():
    return import_backup()


# --- v0.1.30 direct-to-Zotero bridge ---------------------------------------
# This deliberately mirrors the older dedicated Zotero connector app. The
# standard Zotero Connector endpoints create a normal save session/parent item,
# while the companion Zotero helper turns the supplied HTML into a real Snapshot
# and runs Recognize Document on PDFs.
CONNECTOR_API_VERSION='3'
CONNECTOR_BASES=('http://127.0.0.1:23119/connector','http://localhost:23119/connector')
DIRECT_TEMP=APPDIR/'zotero-direct-temp'
DIRECT_TEMP.mkdir(parents=True,exist_ok=True)
PDF_MARKER='ZoteroLocalFileConnector-v0.7'
SNAPSHOT_MARKER='ZoteroLocalFileConnectorSnapshot-v0.8.5'

def _connector_http(url, method='POST', headers=None, data=b'', timeout=90):
    req=urllib.request.Request(url=url,data=data,headers=headers or {},method=method)
    try:
        with urllib.request.urlopen(req,timeout=timeout) as resp:
            return resp.status,dict(resp.headers),resp.read()
    except urllib.error.HTTPError as e:
        body=e.read().decode('utf-8',errors='replace')
        raise RuntimeError(f'Zotero Connector returned HTTP {e.code}: {body[:700]}') from e
    except urllib.error.URLError as e:
        raise RuntimeError(f'Could not connect to Zotero Desktop: {e.reason}') from e

def _find_connector():
    errs=[]
    for base in CONNECTOR_BASES:
        try:
            status,_,_=_connector_http(base+'/ping',method='GET',data=None,timeout=3)
            if 200<=status<300:return base
        except Exception as e:errs.append(str(e))
    raise RuntimeError('Could not connect to Zotero Desktop. Keep Zotero open. '+' | '.join(errs))

def _connector_request(base,endpoint,payload=None,content_type='application/json',raw_data=None,timeout=90,extra_headers=None):
    data=raw_data if raw_data is not None else json.dumps(payload or {}).encode('utf-8')
    headers={'X-Zotero-Connector-API-Version':CONNECTOR_API_VERSION,'Content-Type':content_type}
    if extra_headers:headers.update(extra_headers)
    return _connector_http(base+'/'+endpoint,headers=headers,data=data,timeout=timeout)

def _selected_zotero_target(base):
    _,_,body=_connector_request(base,'getSelectedCollection',{'switchToReadableLibrary':True},timeout=20)
    try:result=json.loads(body.decode('utf-8'))
    except Exception:raise RuntimeError('Zotero returned an unreadable selected collection.')
    selected=result.get('id')
    if not selected:selected='L'+str(result.get('libraryID'))
    elif not isinstance(selected,str):selected='C'+str(selected)
    return result,selected

def zotero_targets():
    helper_version=require_zotero_helper_version()
    base=_find_connector()
    selected,target=_selected_zotero_target(base)
    return {'targets':selected.get('targets') or [],'selectedTargetID':target,'libraryEditable':selected.get('libraryEditable',True),'helperVersion':helper_version,'protocolVersion':STASHLIBRARY_PROTOCOL_VERSION}

def _file_url_path(url):
    parsed=urllib.parse.urlparse(url)
    path=urllib.parse.unquote(parsed.path)
    if parsed.netloc and parsed.netloc.lower()!='localhost':return '\\\\'+parsed.netloc+path.replace('/','\\')
    if len(path)>=3 and path[0]=='/' and path[2]==':':path=path[1:]
    return path.replace('/','\\') if os.name=='nt' else path

def _connector_creators(names):
    out=[]
    for raw in names or []:
        name=re.sub(r'^\s*(?:by\s+)?','',str(raw or ''),flags=re.I).strip()
        if not name:continue
        if ',' in name:
            last,first=(name.split(',',1)+[''])[:2]
            out.append({'firstName':first.strip(),'lastName':last.strip(),'creatorType':'author'})
        else:
            parts=name.split()
            if len(parts)>1:out.append({'firstName':' '.join(parts[:-1]),'lastName':parts[-1],'creatorType':'author'})
            else:out.append({'firstName':'','lastName':name,'creatorType':'author'})
    return out[:50]

def _direct_temp_html(snapshot_html=None,source_path=None):
    name='snapshot-'+secrets.token_hex(12)+'.html'; dest=DIRECT_TEMP/name
    if source_path:shutil.copy2(str(source_path),str(dest))
    else:dest.write_text(str(snapshot_html or ''),encoding='utf-8',newline='')
    return dest

def _direct_save_webpage(url,browser_title,snapshot_html=None,local_html=None,accessed_at=None,target=None):
    base=_find_connector();selected,default_target=_selected_zotero_target(base);target=target or default_target
    temp=_direct_temp_html(snapshot_html=snapshot_html,source_path=local_html)
    meta=extract_archive_metadata(temp)
    fallback_title=Path(local_html).stem if local_html else 'Web Page'
    title=str(meta.get('title') or browser_title or fallback_title).strip() or 'Web Page'
    creators=_connector_creators(meta.get('authors') or [])
    session_id='session-'+secrets.token_hex(12);parent_id='item-'+secrets.token_hex(12)
    item={
        'id':parent_id,'itemType':'webpage','title':title,'url':url,
        'accessDate':'CURRENT_TIMESTAMP','creators':creators,'tags':[],'attachments':[],
        'extra':'\n'.join([SNAPSHOT_MARKER,'Snapshot-Path: '+str(temp),'Snapshot-URL: '+str(url or '')])
    }
    _connector_request(base,'saveItems',{'sessionID':session_id,'uri':url,'items':[item]},timeout=60)
    _connector_request(base,'updateSession',{'sessionID':session_id,'target':target,'tags':[],'note':''},timeout=60)
    return {'sessionID':session_id,'title':title,'parentTitle':title,'contentType':'text/html','selectedTargetID':target,'snapshotQueued':True,'authors':meta.get('authors') or []}

def _direct_pdf_bytes(url,browser_title,pdf_base64=None,pdf_file_name=None):
    if pdf_base64:
        try:data=base64.b64decode(pdf_base64,validate=True)
        except Exception as e:raise RuntimeError('Firefox supplied an unreadable PDF payload.') from e
        name=str(pdf_file_name or 'document.pdf')
    else:
        path=Path(_file_url_path(url))
        if not path.is_file():raise RuntimeError(f'Local file not found: {path}')
        data=path.read_bytes();name=path.name
    if b'%PDF-' not in data[:1024]:raise RuntimeError('The selected file is not a valid PDF.')
    if not name.lower().endswith('.pdf'):name+='.pdf'
    return data,name

def _direct_save_pdf(url,browser_title,pdf_base64=None,pdf_file_name=None,target=None,accessed_at=None,source_url=None,local_path=None):
    base=_find_connector();selected,default_target=_selected_zotero_target(base);target=target or default_target
    if local_path:
        lp=Path(local_path)
        if not lp.is_file(): raise RuntimeError(f'Local PDF not found: {lp}')
        data=lp.read_bytes(); file_name=lp.name
        if b'%PDF-' not in data[:1024]: raise RuntimeError('The selected file is not a valid PDF.')
    else:
        data,file_name=_direct_pdf_bytes(url,browser_title,pdf_base64,pdf_file_name)
    session_id='session-'+secrets.token_hex(12);parent_id='item-'+secrets.token_hex(12)
    title=str(browser_title or '').strip() or Path(file_name).stem or 'Document'
    provenance_url=_zotero_source_url(source_url if source_url is not None else url)
    provenance_accessed=str(accessed_at or '').strip()
    extra_lines=[PDF_MARKER]
    if provenance_url: extra_lines.append('LFB-Provenance-URL: '+provenance_url)
    if provenance_accessed: extra_lines.append('LFB-Provenance-Accessed: '+provenance_accessed)
    item={'id':parent_id,'itemType':'document','title':title,'creators':[],'tags':[],'attachments':[],'extra':'\n'.join(extra_lines)}
    transport_url=provenance_url or ('' if local_path else str(url or ''))
    session_uri=transport_url
    _connector_request(base,'saveItems',{'sessionID':session_id,'uri':session_uri,'items':[item]},timeout=60)
    _connector_request(base,'updateSession',{'sessionID':session_id,'target':target,'tags':[],'note':''},timeout=60)
    metadata={'sessionID':session_id,'parentItemID':parent_id,'title':title,'url':transport_url}
    # http.client encodes header values as Latin-1. Keep this JSON header ASCII-
    # safe so titles/URLs containing curly quotes, dashes, or non-Latin text do
    # not fail during transmission; JSON.parse restores the original Unicode.
    headers={'X-Metadata':json.dumps(metadata,ensure_ascii=True)}
    _connector_request(base,'saveAttachment',content_type='application/pdf',raw_data=data,timeout=120,extra_headers=headers)
    return {'sessionID':session_id,'title':title,'fileName':file_name,'contentType':'application/pdf','selectedTargetID':target,'recognitionQueued':True,'sourceURL':provenance_url,'accessedAt':provenance_accessed}

def _zotero_source_url(value):
    """Return a bibliographic URL, never a private filesystem location."""
    value=str(value or '').strip()
    if not value:return ''
    if re.match(r'^file:',value,re.I) or re.match(r'^[a-zA-Z]:[\\/]',value) or value.startswith('\\\\'):
        return ''
    return value


def zotero_direct_save(url,title='',snapshot_html=None,pdf_base64=None,pdf_file_name=None,accessed_at=None,target=None):
    url=str(url or '').strip();title=str(title or '').strip()
    if not url:raise RuntimeError('No page or file URL was supplied.')
    parsed=urllib.parse.urlparse(url);scheme=parsed.scheme.lower()
    if pdf_base64:return _direct_save_pdf(url,title,pdf_base64,pdf_file_name,target=target,accessed_at=accessed_at,source_url=url)
    if scheme=='file':
        path=Path(_file_url_path(url))
        if not path.is_file():raise RuntimeError(f'Local file not found: {path}')
        low=path.suffix.lower()
        if low=='.pdf':return _direct_save_pdf(url,title,target=target,accessed_at=accessed_at,source_url='')
        if low in {'.html','.htm'}:return _direct_save_webpage('',title,local_html=path,accessed_at=accessed_at,target=target)
        raise RuntimeError('Send direct to Zotero supports local PDF, HTML and HTM files.')
    if scheme in {'http','https'}:
        if snapshot_html:return _direct_save_webpage(url,title,snapshot_html=snapshot_html,accessed_at=accessed_at,target=target)
        raise RuntimeError('A live webpage must be captured before it is sent to Zotero.')
    raise RuntimeError('Unsupported page/file URL.')

def zotero_direct_url(url,title='',authors=None,accessed_at=None,target=None):
    """Ask Zotero itself to download/capture a live HTTP(S) URL.

    This is the preferred fast path for Send Current Page to Zotero. Firefox
    only falls back to browser-side PDF/SingleFile capture if Zotero cannot
    retrieve the URL directly (for example an authenticated/browser-only page).
    """
    url=str(url or '').strip()
    if not re.match(r'^https?://',url,re.I):
        raise RuntimeError('Zotero direct URL capture requires an HTTP or HTTPS URL.')
    return zotero_plugin_request(
        '/local-file-connector/import-remote-url',
        {
            'url':url,
            'title':str(title or ''),
            'authors':authors or [],
            'accessedAt':str(accessed_at or ''),
            'target':target or ''
        },
        timeout=180
    )


def _zotero_plugin_request_raw(endpoint, payload=None, timeout=120):
    data=json.dumps(payload or {}).encode('utf-8')
    last=None
    for base in ('http://127.0.0.1:23119','http://localhost:23119'):
        try:
            req=urllib.request.Request(base+endpoint,data=data,headers={'Content-Type':'application/json'},method='POST')
            with urllib.request.urlopen(req,timeout=timeout) as resp:
                raw=resp.read()
            result=json.loads(raw.decode('utf-8'))
            if not result.get('ok'): raise RuntimeError(result.get('error') or 'Zotero helper returned an error.')
            return result
        except Exception as e:last=e
    raise RuntimeError(
        f'Could not connect to the StashLibrary Zotero helper v{ZOTERO_HELPER_VERSION}. '
        f'Install INSTALL-IN-ZOTERO-StashLibrary-Helper-v{ZOTERO_HELPER_VERSION}.xpi, restart Zotero, and keep Zotero open. '+str(last or '')
    )

def zotero_helper_info():
    result=_zotero_plugin_request_raw('/local-file-connector/version',{},30)
    return {
        'version':str(result.get('version') or '').strip(),
        'protocolVersion':int(result.get('protocolVersion') or 0),
    }

def zotero_helper_version():
    return zotero_helper_info()['version']

def zotero_status():
    """Reliable local compatibility probe used by Configuration/update gating.

    The old probe used a single 350 ms request to 127.0.0.1. On a freshly
    restarted or busy Zotero process that could time out even though the StashLibrary
    Zotero Helper was installed correctly. Use the same two loopback names as
    normal Zotero operations, allow a realistic short timeout, and preserve a
    small diagnostic reason for the UI/debug report.
    """
    data=b'{}'
    errors=[]
    for base in ('http://127.0.0.1:23119','http://localhost:23119'):
        try:
            req=urllib.request.Request(
                base+'/local-file-connector/version',
                data=data,
                headers={'Content-Type':'application/json'},
                method='POST'
            )
            with urllib.request.urlopen(req,timeout=1.5) as resp:
                raw=resp.read()
            result=json.loads(raw.decode('utf-8'))
            actual=str(result.get('version') or '').strip() if result.get('ok') else ''
            protocol=int(result.get('protocolVersion') or 0) if result.get('ok') else 0
            compatible=bool(actual) and protocol==STASHLIBRARY_PROTOCOL_VERSION
            return {
                'connected':compatible,
                'helperVersion':actual,
                'protocolVersion':protocol,
                'compatible':compatible,
                'zoteroReachable':True,
                'statusReason':'connected' if compatible else 'helper-incompatible',
                'statusDetail':'' if compatible else 'The StashLibrary Zotero Helper answered, but its compatibility protocol does not match this StashLibrary release.',
            }
        except Exception as e:
            errors.append(f'{base}: {e}')

    # Distinguish "Zotero is closed/unreachable" from "Zotero is running but
    # this helper endpoint is missing/not ready". This fallback never marks the
    # helper as connected; it only gives the setup UI a useful explanation.
    zotero_reachable=False
    for base in CONNECTOR_BASES:
        try:
            status,_,_=_connector_http(base+'/ping',method='GET',data=None,timeout=0.75)
            if 200 <= status < 500:
                zotero_reachable=True
                break
        except Exception:
            pass
    return {
        'connected':False,
        'helperVersion':'',
        'protocolVersion':0,
        'compatible':False,
        'zoteroReachable':zotero_reachable,
        'statusReason':'helper-not-responding' if zotero_reachable else 'zotero-not-running',
        'statusDetail':('Zotero is running, but the StashLibrary Zotero Helper endpoint did not answer. Restart Zotero after installing the XPI.'
                        if zotero_reachable else 'Zotero is not reachable on its local connector port. Open Zotero and keep it running.'),
        'probeError':' | '.join(errors)[-1200:],
    }

def require_zotero_helper_version():
    info=zotero_helper_info()
    actual=info['version']
    protocol=info['protocolVersion']
    if protocol != STASHLIBRARY_PROTOCOL_VERSION:
        raise RuntimeError(
            f'StashLibrary Zotero helper is incompatible. This StashLibrary release requires protocol {STASHLIBRARY_PROTOCOL_VERSION}, '
            f'but Zotero is running {f"v{actual}" if actual else "an unknown/older helper"} '
            f'(protocol {protocol or "unknown"}). Install the current StashLibrary-Zotero-Helper.xpi and restart Zotero.'
        )
    return actual

def zotero_plugin_request(endpoint, payload=None, timeout=120):
    # Every StashLibrary-to-Zotero operation requires a companion using the same
    # compatibility protocol. Component versions may differ when the protocol
    # has not changed.
    if endpoint != '/local-file-connector/version':
        require_zotero_helper_version()
    return _zotero_plugin_request_raw(endpoint,payload,timeout)

def zotero_manifest_file(root): return root/'.local-bookmarks-zotero-import.json'
def load_zotero_manifest(root):
    try:return json.loads(zotero_manifest_file(root).read_text(encoding='utf-8'))
    except:return {'version':1,'entries':{}}
def save_zotero_manifest(root,manifest):
    manifest['version']=1;manifest['updatedAt']=datetime.now().isoformat(timespec='seconds')
    zotero_manifest_file(root).write_text(json.dumps(manifest,indent=2,ensure_ascii=False),encoding='utf-8')

def ensure_named_folder(parent,name,folder_id=None,claimed=None,imported_at=None):
    parent=Path(parent);parent.mkdir(parents=True,exist_ok=True)
    display=str(name or 'Untitled collection').strip() or 'Untitled collection'
    claimed=claimed if claimed is not None else {}

    # Reuse a folder previously associated with this Zotero collection.
    if folder_id is not None:
        fid=str(folder_id)
        for p in visible_entries(parent):
            if not p.is_dir():continue
            meta=get_item_metadata(p)
            if str(meta.get('zoteroCollectionID') or '')==fid:
                set_item_metadata(parent,p.name,title=display,extra={'zoteroCollectionID':fid})
                claimed[str(p).lower()]=folder_id
                return p,False

    candidate=short_id_dest(parent,'')
    while str(candidate).lower() in claimed and claimed[str(candidate).lower()]!=folder_id:
        # short_id_dest already checks disk/history; claimed covers this same
        # in-flight migration before the filesystem view refreshes.
        candidate=short_id_dest(parent,'')
    claimed[str(candidate).lower()]=folder_id

    candidate.mkdir()
    add_order(parent,candidate.name)

    created_ns=time.time_ns()
    created_at=datetime.fromtimestamp(created_ns/1_000_000_000).astimezone().isoformat()
    set_item_metadata(
        parent,
        candidate.name,
        title=display,
        extra={
            'folderCreatedAt':created_at,
            'folderCreatedNs':str(created_ns),
            'zoteroCollectionID':str(folder_id) if folder_id is not None else ''
        }
    )
    return candidate,True



def _convert_directory_to_short_ids(folder):
    """Convert an entire StashLibrary directory tree to short physical IDs.

    The current directory is converted first. After each child receives its
    new short physical name, recursion continues into the child's NEW path.
    This ensures every branch of the StashLibrary library is converted, not just the
    first/previously-open subtree.
    """
    folder=Path(folder)

    children=visible_entries(folder)
    if not children:
        return 0

    meta=load_metadata(folder)
    old_order=load_order(folder) or [p.name for p in children]

    records=[]
    changed=0

    # Phase 1: move all immediate user entries to unique temporary names.
    # Dot-prefixed temp entries are hidden from visible_entries(), so the
    # short-ID allocator sees a clean namespace for this folder.
    for p in children:
        old_name=p.name
        display=get_display_name(p)
        entry=dict(meta.get(old_name) or {})
        if not entry.get('title'):
            entry['title']=display

        ext='' if p.is_dir() else p.suffix
        is_dir=p.is_dir()

        temp_name=f'.stashlibrary-convert-{uuid.uuid4().hex}{ext}'
        temp=folder/temp_name
        p.rename(temp)

        records.append({
            'old_name':old_name,
            'temp':temp,
            'ext':ext,
            'meta':entry,
            'is_dir':is_dir
        })

    # Phase 2: assign final short IDs, preserving the original StashLibrary order.
    by_old={r['old_name']:r for r in records}
    ordered_records=[]
    for old_name in old_order:
        if old_name in by_old:
            ordered_records.append(by_old.pop(old_name))
    ordered_records.extend(by_old.values())

    new_meta={}
    new_order=[]
    child_dirs=[]

    for rec in ordered_records:
        target=short_id_dest(folder,rec['ext'])
        rec['temp'].rename(target)

        new_meta[target.name]=rec['meta']
        new_order.append(target.name)

        if target.name!=rec['old_name']:
            changed+=1

        if rec['is_dir']:
            child_dirs.append(target)

    save_metadata(folder,new_meta)
    save_order(folder,new_order)

    # Phase 3: recurse into every newly-renamed child directory.
    for child in child_dirs:
        changed += _convert_directory_to_short_ids(child)

    return changed


def _is_short_internal_name(path):
    p=Path(path)
    stem=p.name if p.is_dir() else p.stem
    return bool(re.fullmatch(r'[0-9a-z]+',stem,re.I))

def _count_non_short_entries(folder):
    folder=Path(folder)
    count=0
    for p in visible_entries(folder):
        if not _is_short_internal_name(p):
            count+=1
        if p.is_dir():
            count+=_count_non_short_entries(p)
    return count


def convert_library_to_short_ids():
    root=bookmarks_path()
    if not root:raise RuntimeError('Choose a bookmarks folder first.')
    root=root.resolve()

    # Existing Undo/Redo paths refer to pre-conversion physical names.
    _clear_all_history()

    changed=_convert_directory_to_short_ids(root)
    remaining=_count_non_short_entries(root)

    if remaining:
        raise RuntimeError(
            f'Conversion finished partially, but {remaining} visible entr'
            f'{"y" if remaining==1 else "ies"} still do not use short internal IDs.'
        )

    try:rebuild_database_from_recovery()
    except Exception:pass

    cfg=read_config()
    cfg['storage_layout']='short-id-v1'
    cfg['storage_layout_converted_at']=capture_timestamp()
    write_config(cfg)

    return {
        'changed':changed,
        'remaining':remaining,
        'path':str(root),
        'layout':'short-id-v1'
    }


def zotero_inventory():
    inv=zotero_plugin_request('/local-file-connector/export-attachments',{},180)
    return {'collections':inv.get('collections') or [], 'attachments':inv.get('attachments') or []}

def zotero_migrate(selected_collection_ids=None, selected_attachment_ids=None, destination_path=None, direct_attachment_ids=None):
    library_root=bookmarks_path()
    if not library_root: raise RuntimeError('Choose a bookmarks folder from Settings first.')
    library_root.mkdir(parents=True,exist_ok=True)
    root=Path(destination_path) if destination_path else library_root
    try:
        root=root.resolve(); lib=library_root.resolve(); root.relative_to(lib)
    except Exception:
        raise RuntimeError('The selected migration destination must be inside your StashLibrary bookmarks folder.')
    if not root.exists() or not root.is_dir():
        raise RuntimeError('The selected StashLibrary destination folder no longer exists.')
    started=time.monotonic();progress('Zotero migration','connecting','Reading Zotero inventory',0,started)
    inv=zotero_inventory()
    collections=inv.get('collections') or []; attachments=inv.get('attachments') or []
    selected_collections={int(x) for x in (selected_collection_ids or []) if str(x).isdigit()}
    selected_attachments={str(x) for x in (selected_attachment_ids or [])}
    direct_attachments={str(x) for x in (direct_attachment_ids or [])}
    if selected_collections or selected_attachments:
        attachments=[a for a in attachments if str(a.get('id')) in selected_attachments or str(a.get('key')) in selected_attachments]
    else:
        attachments=[]
    coll_by_id={int(c['id']):c for c in collections if c.get('id') is not None}
    folder_paths={};claimed={};folders_created=0
    def folder_for(cid,stack=None):
        nonlocal folders_created
        cid=int(cid)
        if cid in folder_paths:return folder_paths[cid]
        c=coll_by_id.get(cid)
        if not c:return root
        stack=set(stack or ())
        if cid in stack:return root
        stack.add(cid)
        pid=c.get('parentID')
        parent=folder_for(int(pid),stack) if pid else root
        path,created=ensure_named_folder(parent,c.get('name') or 'Untitled collection',cid,claimed)
        folders_created+=1 if created else 0;folder_paths[cid]=path
        return path
    # Recreate selected collection folders and their required parent hierarchy.
    collections_to_create=set(selected_collections)
    def add_ancestors(cid):
        seen=set()
        while cid and cid not in seen:
            seen.add(cid); collections_to_create.add(cid)
            c=coll_by_id.get(cid); pid=c.get('parentID') if c else None
            cid=int(pid) if pid else None
    for cid in list(collections_to_create): add_ancestors(cid)
    selected_collection_rows=[c for c in collections if int(c.get('id')) in collections_to_create]
    for i,c in enumerate(selected_collection_rows):
        try:folder_for(int(c['id']))
        except Exception:pass
        if selected_collection_rows and i%25==0:progress('Zotero migration','creating folders',f'{i+1}/{len(selected_collection_rows)} collections',min(20,(i+1)/len(selected_collection_rows)*20),started)
    manifest=load_zotero_manifest(root);entries=manifest.setdefault('entries',{})
    copied=updated=skipped=missing=failed=0;errors=[]
    total=max(1,len(attachments))
    for i,a in enumerate(attachments):
        src_text=a.get('path') or ''; src=Path(src_text) if src_text else None
        collection_ids=[int(x) for x in (a.get('collectionIDs') or []) if str(x).isdigit()]
        attachment_identifiers={str(a.get('id')),str(a.get('key'))}
        is_direct=bool(attachment_identifiers & direct_attachments)
        if is_direct:
            # An individually selected file goes exactly into the StashLibrary folder
            # chosen in the migration dialog. Do not silently create its Zotero
            # collection underneath the chosen destination.
            destinations=[root]
        else:
            chosen_ids=[cid for cid in collection_ids if cid in collections_to_create and cid in coll_by_id]
            destinations=[folder_for(cid) for cid in chosen_ids]
            if not destinations:
                # A selected unfiled file is also copied directly into the chosen
                # destination; there is no need for a surprise _Zotero Unfiled folder.
                destinations=[root]
        if not src or not src.is_file():
            missing+=1
            item_name=a.get('parentTitle') or a.get('title') or a.get('key') or 'attachment'
            errors.append(f'Skipped: "{item_name}" — no local attachment file was available')
            continue
        try: st=src.stat()
        except Exception as e:
            failed+=1;errors.append(f"Cannot read {src.name}: {e}");continue
        for dest_folder in destinations:
            identity=f"{a.get('libraryID','')}:{a.get('key') or a.get('id')}|{str(dest_folder).lower()}"
            rec=entries.get(identity) or {}
            old_dest=Path(rec['dest']) if rec.get('dest') else None
            # StashLibrary v0.5 stores a short internal physical filename. Zotero's
            # bibliographic title, source URL and Accessed date stay in metadata.
            display_title=str(a.get('parentTitle') or a.get('title') or src.stem).strip() or src.stem
            source_url=str(a.get('sourceURL') or a.get('sourceUrl') or '').strip()
            accessed_at=str(a.get('accessDate') or a.get('accessedAt') or '').strip()

            same=bool(
                old_dest and old_dest.exists() and old_dest.parent.resolve()==dest_folder.resolve()
                and rec.get('size')==st.st_size and rec.get('mtime_ns')==st.st_mtime_ns
            )
            if same:
                set_item_metadata(
                    dest_folder,old_dest.name,title=display_title,
                    source_url=source_url,accessed_at=accessed_at
                )
                skipped+=1
                continue

            try:
                if old_dest and old_dest.exists() and old_dest.parent.resolve()==dest_folder.resolve():
                    target=old_dest
                else:
                    target=short_id_dest(dest_folder,src.suffix)

                copy_file_with_progress(src,target,'Zotero migration')

                if old_dest and old_dest.exists() and target.resolve()!=old_dest.resolve():
                    try:
                        old_name=old_dest.name
                        old_dest.unlink()
                        order=load_order(dest_folder)
                        if old_name in order:
                            order=[target.name if x==old_name else x for x in order]
                            save_order(dest_folder,order)
                        remove_display_name(dest_folder,old_name)
                    except Exception:
                        pass

                if not old_dest or target!=old_dest:
                    add_order(dest_folder,target.name)

                set_item_metadata(
                    dest_folder,target.name,title=display_title,
                    source_url=source_url,accessed_at=accessed_at
                )

                if rec:updated+=1
                else:copied+=1

                entries[identity]={
                    'dest':str(target),'source':str(src),'size':st.st_size,
                    'mtime_ns':st.st_mtime_ns,'zoteroKey':a.get('key'),
                    'zoteroID':a.get('id'),'title':a.get('title',''),
                    'parentTitle':a.get('parentTitle',''),'sourceURL':source_url,
                    'accessDate':accessed_at
                }
            except Exception as e:
                failed+=1;errors.append(f"{src.name} → {dest_folder.name}: {e}")
        pct=20+((i+1)/total*80);progress('Zotero migration','copying attachments',f'{i+1}/{len(attachments)} — {src.name if src else a.get("title","")}',pct,started)
        if i%10==0: save_zotero_manifest(root,manifest)
    save_zotero_manifest(root,manifest)
    progress('Zotero migration','finished',f'{copied} copied, {updated} updated, {skipped} unchanged',100,started)
    return {'copied':copied,'updated':updated,'skipped':skipped,'missing':missing,'failed':failed,'foldersCreated':folders_created,'attachments':len(attachments),'collections':len(collections),'errors':errors[:50]}

def send_to_zotero(path,target=None):
    original_ref=_normalize_virtual_ref(path)

    # In flat mode, read the logical bookmark metadata BEFORE resolving the
    # random-number archive payload. Otherwise get_item_metadata() sees the
    # opaque physical filename and the random number can leak into Zotero.
    if is_flat_layout() and original_ref.startswith(VIRTUAL_ROOT):
        def op(conn):
            row=_flat_row(conn,original_ref)
            if not row or row['kind']!='bookmark':
                raise RuntimeError('StashLibrary bookmark no longer exists.')
            p=_flat_payload_path_from_row(row)
            if not p.is_file():
                raise RuntimeError(f'Archived file is missing: {p.name}')
            return p,_row_to_meta(row),str(row['display_name'] or '')
        p,meta,logical_title=_with_db(op)
    else:
        p=Path(path)
        if not p.is_file():raise RuntimeError('Only individual bookmark files can be sent to Zotero.')
        meta=get_item_metadata(p)
        logical_title=str(meta.get('title') or '')

    source_url=_zotero_source_url(meta.get('sourceUrl'))
    accessed_at=str(meta.get('accessedAt') or '').strip()
    if not accessed_at:
        try:
            accessed_at=datetime.fromtimestamp(p.stat().st_mtime).astimezone().isoformat(timespec='seconds')
        except Exception:
            accessed_at=capture_timestamp()

    display_title=str(logical_title or meta.get('title') or p.stem).strip() or p.stem

    if p.suffix.lower()=='.pdf':
        # Exact companion-version compatibility is enforced for every Zotero
        # operation by zotero_plugin_request().
        require_zotero_helper_version()
        file_url=p.resolve().as_uri()
        result=_direct_save_pdf(
            file_url,display_title,target=target,accessed_at=accessed_at,
            source_url=source_url,local_path=p
        )
        return {
            'attachmentTitle':display_title,'parentTitle':display_title,
            'recognized':False,'usedArchiveMetadata':False,
            'sourceURL':source_url,'accessedAt':accessed_at,**result
        }

    archived=extract_archive_metadata(p) if p.suffix.lower() in {'.html','.htm'} else {'title':'','authors':[]}
    result=zotero_plugin_request('/local-file-connector/import-file',{
        'path':str(p),
        'sourceURL':source_url,
        'accessedAt':accessed_at,
        'displayTitle':display_title,
        'archiveTitle':archived.get('title') or '',
        'archiveAuthors':archived.get('authors') or [],
        'target':target or ''
    },180)
    return {
        'itemID':result.get('itemID'),'itemKey':result.get('itemKey'),
        'attachmentTitle':result.get('attachmentTitle') or display_title,
        'parentID':result.get('parentID'),'parentKey':result.get('parentKey'),
        'parentTitle':result.get('parentTitle') or display_title,
        'recognized':bool(result.get('recognized')),
        'usedArchiveMetadata':bool(result.get('usedArchiveMetadata')),
        'sourceURL':source_url,'accessedAt':accessed_at
    }


def send_current_page_to_zotero(url,title,authors=None,accessed_at=None):
    payload={
        'url':str(url or ''),
        'title':str(title or ''),
        'authors':authors or [],
        'accessedAt':str(accessed_at or '')
    }
    result=zotero_plugin_request('/local-file-connector/import-current-page',payload,120)
    if not result.get('ok'):
        raise RuntimeError(result.get('error') or 'Zotero could not import the current page')
    return result

def open_archive_file(physical_name):
    """Open an unlinked physical archive file through StashLibrary's friendly local viewer."""
    name=Path(str(physical_name or '')).name
    if not name or name!=str(physical_name or ''):
        raise RuntimeError('Invalid archive filename.')
    arc=flat_archive_dir()
    p=arc/name
    if not p.is_file():raise RuntimeError(f'Physical file is missing: {name}')
    return friendly_open_url(str(p),p.stem)


def backup_info():
    b=bookmarks_path(); z=backups_path(); latest=latest_backup()
    u=undo_root()
    return {'hostVersion':STASHLIBRARY_VERSION,'protocolVersion':STASHLIBRARY_PROTOCOL_VERSION,'root':str(b) if b else '', 'bookmarks':str(b) if b else '', 'internalData':str(internal_data_dir(create=False) or ''), 'backups':str(z) if z else '', 'undoStorage':str(u) if u else '', 'latest':str(latest) if latest else '', 'backupActive':BACKUP_RUN_LOCK.locked(), 'webdav':webdav_backup_info(), **history_state()}

def open_database():
    root=bookmarks_path()
    if not root:raise RuntimeError('Choose a bookmarks folder first.')
    p=database_file(root)
    if not p:raise RuntimeError('StashLibrary database path is unavailable.')
    p=Path(p)
    if not p.exists():raise RuntimeError('StashLibrary database file does not exist yet.')
    _open_explorer_foreground(p,select=True)
    return p


def open_folder(kind):
    if kind in {'undo','data'}:
        p=internal_data_dir()
    else:
        p=bookmarks_path() if kind=='bookmarks' else backups_path()
    if not p:raise RuntimeError(f'Choose a {kind} folder first.')
    p.mkdir(parents=True,exist_ok=True)
    _open_explorer_foreground(p,select=False)
    return p

def schedule_windows_helper_uninstall():
    """Remove StashLibrary's native-messaging registration and clean helper binaries.

    The user's self-contained StashLibrary library, machine configuration, and backups are
    intentionally preserved. The currently running helper cannot delete its own
    executable, so a hidden detached PowerShell process waits for this Python
    host and its launcher to exit, then removes only StashLibrary's helper/runtime
    directories from LOCALAPPDATA.
    """
    if os.name!='nt':
        raise RuntimeError('Windows Helper uninstall is only available on Windows.')

    local=Path(os.environ.get('LOCALAPPDATA',str(Path.home())))
    installed_uninstaller=local/'StashLibrary'/'Installer'/'StashLibrary-Windows-Helper.exe'
    if installed_uninstaller.is_file():
        flags=getattr(subprocess,'CREATE_NO_WINDOW',0)|getattr(subprocess,'DETACHED_PROCESS',0)
        try:
            subprocess.Popen(
                [str(installed_uninstaller),'--uninstall','--quiet'],
                stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
                close_fds=True,creationflags=flags
            )
            return {
                'scheduled':True,
                'keptLibrary':str(bookmarks_path() or ''),
                'keptBackups':str(backups_path() or ''),
                'message':'Windows Helper uninstall scheduled. StashLibrary library, settings, and backups are preserved.'
            }
        except Exception:
            # Fall through to the self-cleaning fallback below.
            pass

    import winreg
    native_key=r'Software\Mozilla\NativeMessagingHosts\stashlibrary.host'
    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER,native_key)
    except FileNotFoundError:
        pass
    except OSError as e:
        raise RuntimeError(f'Could not remove the Firefox native-messaging registration: {e}') from e

    install_root=local/'StashLibrary'
    helper_root=install_root/'NativeHelper'
    runtime_root=install_root/'Runtime'
    pids=[os.getpid(),os.getppid()]

    def psq(v):
        return "'"+str(v).replace("'","''")+"'"

    pid_list=','.join(str(int(x)) for x in pids if int(x)>0)
    script=f"""
$ErrorActionPreference='SilentlyContinue'
$pidsToWait=@({pid_list})
for($i=0;$i -lt 80;$i++){{
  $alive=$false
  foreach($pidToWait in $pidsToWait){{
    if(Get-Process -Id $pidToWait -ErrorAction SilentlyContinue){{$alive=$true;break}}
  }}
  if(-not $alive){{break}}
  Start-Sleep -Milliseconds 150
}}
$helper={psq(helper_root)}
$runtime={psq(runtime_root)}
$root={psq(install_root)}
for($i=0;$i -lt 40;$i++){{
  Remove-Item -LiteralPath $helper -Recurse -Force -ErrorAction SilentlyContinue
  if(-not (Test-Path -LiteralPath $helper)){{break}}
  Start-Sleep -Milliseconds 250
}}
for($i=0;$i -lt 20;$i++){{
  Remove-Item -LiteralPath $runtime -Recurse -Force -ErrorAction SilentlyContinue
  if(-not (Test-Path -LiteralPath $runtime)){{break}}
  Start-Sleep -Milliseconds 250
}}
try{{
  if((Test-Path -LiteralPath $root) -and -not (Get-ChildItem -LiteralPath $root -Force | Select-Object -First 1)){{
    Remove-Item -LiteralPath $root -Force -ErrorAction SilentlyContinue
  }}
}}catch{{}}
"""
    flags=getattr(subprocess,'CREATE_NO_WINDOW',0)|getattr(subprocess,'DETACHED_PROCESS',0)
    try:
        subprocess.Popen(
            ['powershell.exe','-NoProfile','-NonInteractive','-ExecutionPolicy','Bypass','-WindowStyle','Hidden','-Command',script],
            stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
            close_fds=True,creationflags=flags
        )
    except Exception as e:
        # Restore the registration if cleanup could not even be scheduled. The
        # current manifest lives beside this host executable.
        try:
            manifest=Path(__file__).resolve().parent/'stashlibrary.host.json'
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER,native_key) as key:
                winreg.SetValueEx(key,'',0,winreg.REG_SZ,str(manifest))
        except Exception:
            pass
        raise RuntimeError(f'Could not start the Windows Helper uninstaller: {e}') from e

    return {
        'scheduled':True,
        'keptLibrary':str(bookmarks_path() or ''),
        'keptBackups':str(backups_path() or ''),
        'message':'Windows Helper uninstall scheduled. StashLibrary library, settings, and backups are preserved.'
    }


def debug_info():
    info={
        'hostVersion':STASHLIBRARY_VERSION,
        'protocolVersion':STASHLIBRARY_PROTOCOL_VERSION,
        'python':sys.version.split()[0],
        'platform':sys.platform,
        'storageLayout':read_config().get('storage_layout',''),
        'internalDataLayout':read_config().get('internal_data_layout','legacy-library-v1'),
        'internalDataPath':str(internal_data_dir(create=False) or ''),
        'bookmarksPath':str(bookmarks_path() or ''),
        'archivePath':str(flat_archive_dir() if bookmarks_path() and is_flat_layout() else (bookmarks_path() or '')),
        'backupsPath':str(backups_path() or ''),
        'history':history_state(),
    }
    try:info['database']=database_info()
    except Exception as e:info['databaseError']=str(e)
    try:
        t=get_tree();info['treeRootItems']=len((t or {}).get('children') or [])
    except Exception as e:info['treeError']=str(e)
    return info


def _validate_delete_target(path):
    p=Path(path)
    root=bookmarks_path()
    if not root:raise RuntimeError('Choose a bookmarks folder from Settings first.')
    if not p.exists():
        raise RuntimeError(f'Cannot delete because the item no longer exists: {p}')
    try:
        pr=p.resolve()
        rr=root.resolve()
        if pr==rr:
            raise RuntimeError('StashLibrary cannot delete the bookmarks root folder itself.')
        pr.relative_to(rr)
    except ValueError:
        raise RuntimeError('StashLibrary refused to delete an item outside the selected bookmarks folder.')
    # Internal StashLibrary data is never a user bookmark.
    try:
        if any(name in p.relative_to(root).parts for name in (SYSTEM_DIR_NAME,UNIFIED_DATA_DIR_NAME,LEGACY_UNIFIED_DATA_DIR_NAME,CLOUD_META_DIR)):
            raise RuntimeError('StashLibrary internal data cannot be deleted as a bookmark.')
    except ValueError:
        pass
    return p

def handle(m):
    cmd=m.get('cmd');rid=m.get('id')
    library_mutating_commands={
        'mkdir','delete','add','save_pdf_data','import_download','reorder','move','copy','rename','rename_physical_file',
        'undo','redo','zotero_migrate','import_backup','import_webdav_backup_folder','repair_apply','repair_rescan_resync','repair_flat_catalogue',
        'repair_catalogue_file_links','rebuild_database','convert_short_ids','convert_flat_archive','readable_flat_filenames'
    }
    try:
        if cmd=='tree':
            progress('Reading bookmarks','loading SQLite catalogue');r=root_path();t=get_tree();count=len(t.get('children',[])) if t else 0;progress('Reading bookmarks','tree ready',f'{count} root items',100);res={'ok':True,'tree':t,'root':str(r) if r else ''}
        elif cmd=='choose_bookmarks':
            progress('Choosing bookmarks folder','folder picker is open');p=choose_folder('bookmarks');res={'ok':bool(p),'path':p or '', 'error':'Folder unchanged' if not p else ''}
        elif cmd=='choose_backups':
            progress('Choosing backup folder','folder picker is open');p=choose_folder('backups');res={'ok':bool(p),'path':p or '', 'error':'Folder unchanged' if not p else ''}
        elif cmd=='webdav_info':res={'ok':True,**webdav_backup_info()}
        elif cmd=='webdav_connect':res={'ok':True,**webdav_connect(m.get('provider'),m.get('url'),m.get('username'),m.get('password'))}
        elif cmd=='webdav_disconnect':res={'ok':True,**webdav_disconnect()}
        elif cmd=='webdav_backup_now':res={'ok':True,**webdav_backup_now('manual')}
        elif cmd=='webdav_replace_with_current':res={'ok':True,**webdav_replace_with_current()}
        elif cmd=='webdav_restore':res={'ok':True,**webdav_restore()}
        elif cmd=='choose_storage_folder':
            p=choose_storage_folder(m.get('mode'));res={'ok':bool(p),'path':p or '', 'mode':storage_sync_mode(), 'error':'Folder unchanged' if not p else ''}
        elif cmd=='choose_cloud_sync':
            p=choose_cloud_sync_folder();res={'ok':bool(p),'path':p or '', 'mode':storage_sync_mode(), 'error':'Folder unchanged' if not p else ''}
        elif cmd=='cloud_sync_info':res={'ok':True,**cloud_sync_info()}
        elif cmd=='cloud_sync_now':res={'ok':True,**cloud_sync_now('manual')}
        elif cmd=='cloud_restore':res={'ok':True,**restore_from_cloud()}
        elif cmd in {'cloud_sync_enable','cloud_sync_disable'}:res={'ok':True,**cloud_sync_info()}
        elif cmd=='open_bookmarks':res={'ok':True,'path':str(open_folder('bookmarks'))}
        elif cmd=='open_cloud_sync':
            p=cloud_sync_path();
            if not p:raise RuntimeError('Choose a StashLibrary folder first.')
            _open_explorer_foreground(p,select=False);res={'ok':True,'path':str(p)}
        elif cmd=='open_archive':
            p=flat_archive_dir();p.mkdir(parents=True,exist_ok=True);_open_explorer_foreground(p,select=False);res={'ok':True,'path':str(p)}
        elif cmd=='open_backups':res={'ok':True,'path':str(open_folder('backups'))}
        elif cmd=='open_undo':res={'ok':True,'path':str(open_folder('undo'))}
        elif cmd=='open_data':res={'ok':True,'path':str(open_folder('data'))}
        elif cmd=='open_database':res={'ok':True,'path':str(open_database())}
        elif cmd=='backup_now':
            try:res={'ok':True,'path':str(create_backup(m.get('reason','manual')))}
            except BackupCancelled:res={'ok':True,'cancelled':True,'path':''}
        elif cmd=='manual_backup_create':
            try:res={'ok':True,**create_manual_backup_interactive()}
            except BackupCancelled:res={'ok':True,'cancelled':True,'path':''}
        elif cmd=='cancel_backup':
            active=BACKUP_RUN_LOCK.locked()
            if active:BACKUP_CANCEL_EVENT.set()
            res={'ok':True,'cancelRequested':active}
        elif cmd=='backup_info':res={'ok':True,**backup_info()}
        elif cmd=='history_state':res={'ok':True,**history_state()}
        elif cmd=='history_group_begin':res={'ok':True,**begin_history_group(m.get('label','Batch operation'))}
        elif cmd=='history_group_end':res={'ok':True,**end_history_group()}
        elif cmd=='undo':res={'ok':True,**undo_action()}
        elif cmd=='redo':res={'ok':True,**redo_action()}
        elif cmd=='send_to_zotero':res={'ok':True,**send_to_zotero(m['path'],m.get('target'))}
        elif cmd=='send_friendly_url_to_zotero':res={'ok':True,**send_friendly_url_to_zotero(m.get('url'),m.get('target'))}
        elif cmd=='send_current_page_to_zotero':res={'ok':True,**send_current_page_to_zotero(m.get('url'),m.get('title'),m.get('authors'),m.get('accessedAt'))}
        elif cmd=='zotero_direct_url':res={'ok':True,**zotero_direct_url(m.get('url'),m.get('title',''),m.get('authors') or [],m.get('accessedAt'),m.get('target'))}
        elif cmd=='zotero_direct_save':res={'ok':True,**zotero_direct_save(m.get('url'),m.get('title',''),m.get('snapshotHTML'),m.get('pdfBase64'),m.get('pdfFileName'),m.get('accessedAt'),m.get('target'))}
        elif cmd=='export_backup':res={'ok':True,**export_backup()}
        elif cmd=='import_backup':res={'ok':True,**import_backup()}
        elif cmd=='import_webdav_backup_folder':res={'ok':True,**import_webdav_backup_folder()}
        elif cmd=='zotero_status':res={'ok':True,**zotero_status()}
        elif cmd=='zotero_targets':res={'ok':True,**zotero_targets()}
        elif cmd=='zotero_inventory':res={'ok':True,**zotero_inventory()}
        elif cmd=='zotero_migrate':res={'ok':True,**(_flat_zotero_migrate(m.get('collectionIDs'),m.get('attachmentIDs'),m.get('destinationPath'),m.get('directAttachmentIDs')) if is_flat_layout() else zotero_migrate(m.get('collectionIDs'),m.get('attachmentIDs'),m.get('destinationPath'),m.get('directAttachmentIDs')))}
        elif cmd=='convert_short_ids':res={'ok':True,**convert_library_to_short_ids(),**history_state()}
        elif cmd=='convert_flat_archive':res={'ok':True,**_flat_convert_existing(),**history_state()}
        elif cmd=='readable_flat_filenames':res={'ok':True,**_make_flat_filenames_readable()}
        elif cmd=='repair_flat_catalogue':res={'ok':True,**_flat_rebuild_database_from_recovery()}
        elif cmd=='database_info':res={'ok':True,**database_info()}
        elif cmd=='repair_catalogue_file_links':res={'ok':True,**repair_catalogue_file_links()}
        elif cmd=='repair_scan':res={'ok':True,**repair_scan()}
        elif cmd=='repair_apply':res={'ok':True,**repair_apply(m.get('issue') or {})}
        elif cmd=='repair_rescan_resync':res={'ok':True,**repair_rescan_resync()}
        elif cmd=='debug_info':res={'ok':True,**debug_info()}
        elif cmd=='uninstall_helper':res={'ok':True,**schedule_windows_helper_uninstall()}
        elif cmd=='open_file_location':res={'ok':True,**_flat_open_physical_location(m['path'])}
        elif cmd=='open_archive_file':res={'ok':True,**open_archive_file(m.get('physicalName',''))}
        elif cmd=='open_alias':res={'ok':True,**friendly_open_url(m.get('path'),m.get('displayName',''))}
        elif cmd=='rebuild_database':res={'ok':True,**(_flat_rebuild_database_from_recovery() if is_flat_layout() else rebuild_database_from_recovery())}
        else:
            r=root_path()
            if not r: raise RuntimeError('Choose a bookmarks folder from Settings first.')
            if cmd=='open':
                if is_flat_layout():
                    opened=friendly_open_url(m['path'],m.get('displayName',''))
                    progress('Opening bookmark','serving canonical archive payload through readable local URL',opened.get('title',''))
                    res={'ok':True,**opened}
                else:
                    progress('Opening bookmark','asking Windows to open file',Path(m['path']).name)
                    os.startfile(m['path'])
                    res={'ok':True}
            elif cmd=='mkdir' and is_flat_layout():
                p=_flat_create_folder(m['parent'],m['name'],True);res={'ok':True,'path':p,**history_state()}
            elif cmd=='mkdir':
                progress('Creating folder','creating on disk',m['name'])
                par=Path(m['parent']);before=load_order(par) or [x.name for x in visible_entries(par)]
                created_ns=time.time_ns()
                p=short_id_dest(par,'')
                created_at=datetime.fromtimestamp(created_ns/1_000_000_000).astimezone().isoformat()
                p.mkdir();add_order(par,p.name)
                set_item_metadata(par,p.name,title=m['name'],extra={'folderCreatedAt':created_at,'folderCreatedNs':str(created_ns)})
                record_action({'type':'created','label':f'Create folder {m["name"]}','path':str(p),'display':m['name'],'meta':get_item_metadata(p),'order_before':before,'order_after':load_order(par)})
                res={'ok':True,'path':str(p),**history_state()}
            elif cmd=='delete' and is_flat_layout():
                _flat_delete(m['path'],True);res={'ok':True,**history_state()}
            elif cmd=='delete':
                p=_validate_delete_target(m['path'])
                par=p.parent
                display=get_display_name(p)
                before=load_order(par) or [x.name for x in visible_entries(par)]
                action={'type':'delete','label':f'Delete {display}','path':str(p),'display':display,'meta':get_item_metadata(p),'order_before':before}
                action_dir=_action_store(action)
                store=action_dir/p.name
                try:
                    remove_display_name(par,p.name)
                    _move_path(p,store)
                    save_order(par,[x.name for x in visible_entries(par)])
                    action['stored']=_encode_history_path(store)
                    action['order_after']=load_order(par)
                    record_action(action)
                    res={'ok':True,**history_state()}
                except Exception:
                    # If deletion failed before the action was recorded, do not leave
                    # an empty recovery directory behind.
                    try:
                        if action_dir.exists() and not any(action_dir.iterdir()):
                            action_dir.rmdir()
                    except Exception:pass
                    raise
            elif cmd=='add' and is_flat_layout():
                ref=_flat_save_url(m['parent'],m['url'],m.get('name',''));res={'ok':True,'path':ref,'physicalName':_flat_physical_name(ref),**history_state()}
            elif cmd=='add':
                par=Path(m['parent']);before=load_order(par) or [x.name for x in visible_entries(par)];p=save_url(par,m['url'],m.get('name',''));record_action({'type':'created','label':f'Add {get_display_name(p)}','path':str(p),'display':get_display_name(p),'meta':get_item_metadata(p),'order_before':before,'order_after':load_order(par)});res={'ok':True,'path':str(p),**history_state()}
            elif cmd=='save_pdf_data' and is_flat_layout():
                ref=_flat_save_pdf_data(m['parent'],m.get('name','document.pdf'),m.get('pdfBase64',''),m.get('displayTitle',''),m.get('url',''));res={'ok':True,'path':ref,'physicalName':_flat_physical_name(ref),**history_state()}
            elif cmd=='save_pdf_data':
                par=Path(m['parent']);before=load_order(par) or [x.name for x in visible_entries(par)];p=save_pdf_data(par,m.get('name','document.pdf'),m.get('pdfBase64',''),m.get('displayTitle',''),m.get('url',''));record_action({'type':'created','label':f'Add {get_display_name(p)}','path':str(p),'display':get_display_name(p),'meta':get_item_metadata(p),'order_before':before,'order_after':load_order(par)});res={'ok':True,'path':str(p),**history_state()}
            elif cmd=='import_download' and is_flat_layout():
                ref=_flat_import_download(m['src'],m['dest'],m.get('displayTitle',''),m.get('url',''));res={'ok':True,'path':ref,'physicalName':_flat_physical_name(ref),**history_state()}
            elif cmd=='import_download':
                par=Path(m['dest']);before=load_order(par) or [x.name for x in visible_entries(par)];p=import_download(m['src'],par,m.get('displayTitle',''),m.get('url',''));record_action({'type':'created','label':f'Add {get_display_name(p)}','path':str(p),'display':get_display_name(p),'meta':get_item_metadata(p),'order_before':before,'order_after':load_order(par)});res={'ok':True,'path':str(p),**history_state()}
            elif cmd=='reorder' and is_flat_layout():res={'ok':True,'path':_flat_reorder(m['src'],m['parent'],m.get('target'),m.get('position','end'),True),**history_state()}
            elif cmd=='reorder':progress('Reordering','updating bookmark order',Path(m['src']).name);res={'ok':True,'path':str(reorder_item(m['src'],m['parent'],m.get('target'),m.get('position','end')))};progress('Reordering','order saved',Path(m['src']).name,100)
            elif cmd=='move' and is_flat_layout():res={'ok':True,'path':_flat_move(m['src'],m['dest'],m.get('target'),m.get('position','end'),True),**history_state()}
            elif cmd=='move':res={'ok':True,'path':str(move_or_copy(m['src'],m['dest'],False,m.get('target'),m.get('position','end')))}
            elif cmd=='copy' and is_flat_layout():res={'ok':True,'path':_flat_copy(m['src'],m['dest'],True),**history_state()}
            elif cmd=='copy':res={'ok':True,'path':str(move_or_copy(m['src'],m['dest'],True,m.get('target'),m.get('position','end')))}
            elif cmd=='rename' and is_flat_layout():res={'ok':True,'path':_flat_rename(m['path'],m['name'],True),**history_state()}
            elif cmd=='rename_physical_file' and is_flat_layout():res={'ok':True,'physicalName':_flat_rename_physical_file(m['path'],m['name'])}
            elif cmd=='rename':progress('Renaming','renaming bookmark',Path(m['path']).name);res={'ok':True,'path':str(rename_bookmark(m['path'],m['name']))}
            else:raise RuntimeError('Unknown command')
    except Exception as e:res={'ok':False,'error':str(e)}
    if rid:res['replyTo']=rid
    send(res)
    if cmd=='uninstall_helper' and res.get('ok'):
        threading.Timer(0.18,lambda:os._exit(0)).start()

def main():
    while True:
        head=sys.stdin.buffer.read(4)
        if not head:break
        n=struct.unpack('<I',head)[0];raw=sys.stdin.buffer.read(n)
        try:m=json.loads(raw.decode('utf-8')); threading.Thread(target=handle,args=(m,),daemon=True).start()
        except Exception as e:send({'ok':False,'error':str(e)})
if __name__=='__main__':main()
