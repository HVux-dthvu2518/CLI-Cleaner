use std::collections::HashSet;
use std::fs::{self, Metadata};
use std::path::Path;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::UNIX_EPOCH;

use pyo3::prelude::*;

#[cfg(windows)]
const FILE_ATTRIBUTE_REPARSE_POINT: u32 = 0x0400;

// Skip reasons are stringified here and parsed by disk_explorer.models.SkipReason.
const SKIP_SYMLINK: &str = "symlink";
const SKIP_REPARSE: &str = "reparse point";
const SKIP_DUPLICATE: &str = "already visited";
const SKIP_NON_REGULAR: &str = "not a regular file";

type EntryRecord = (String, bool, Option<u64>, Option<i64>, String, bool, bool);
type SkipRecord = (String, &'static str);
type ListingRecord = (Vec<EntryRecord>, usize, Vec<String>, bool, bool, Vec<SkipRecord>);
type SizeRecord = (
    u64,
    u64,
    u64,
    usize,
    Vec<String>,
    bool,
    bool,
    Vec<SkipRecord>,
);

/// Directory identity used to detect a directory reached twice in one scan.
///
/// Unix inodes are cheap and always available. Windows has no stable equivalent
/// on every target architecture, so canonical paths are used instead - and only
/// when link traversal is enabled, because that is the only way to form a loop.
#[derive(PartialEq, Eq, Hash)]
enum DirectoryIdentity {
    Inode(u64, u64),
    Canonical(String),
}

#[pyclass]
struct CancellationToken {
    cancelled: Arc<AtomicBool>,
}

#[pymethods]
impl CancellationToken {
    #[new]
    fn new() -> Self {
        Self {
            cancelled: Arc::new(AtomicBool::new(false)),
        }
    }

    fn set(&self) {
        self.cancelled.store(true, Ordering::Relaxed);
    }

    fn is_set(&self) -> bool {
        self.cancelled.load(Ordering::Relaxed)
    }
}

fn is_reparse_point(metadata: &Metadata) -> bool {
    #[cfg(windows)]
    {
        use std::os::windows::fs::MetadataExt;
        metadata.file_attributes() & FILE_ATTRIBUTE_REPARSE_POINT != 0
    }
    #[cfg(not(windows))]
    {
        let _ = metadata;
        false
    }
}

fn skip_reason(is_symlink: bool, reparse: bool, followed: bool) -> Option<&'static str> {
    if followed {
        return None;
    }
    if reparse {
        Some(SKIP_REPARSE)
    } else if is_symlink {
        Some(SKIP_SYMLINK)
    } else {
        None
    }
}

/// Mirrors FileSystemScanner._may_traverse in the Python fallback.
fn may_traverse(is_symlink: bool, reparse: bool, follow_links: bool, reparse_mounts: bool) -> bool {
    if is_symlink {
        follow_links
    } else if reparse {
        reparse_mounts || follow_links
    } else {
        true
    }
}

fn modified_ns(metadata: &Metadata) -> Option<i64> {
    let modified = metadata.modified().ok()?;
    match modified.duration_since(UNIX_EPOCH) {
        Ok(duration) => Some(duration.as_nanos().min(i64::MAX as u128) as i64),
        Err(error) => {
            let nanos = error.duration().as_nanos().min(i64::MAX as u128) as i64;
            Some(-nanos)
        }
    }
}

/// Mirrors Path.suffix in the Python fallback: no dot for directories, for
/// dotfiles or for a trailing dot, and the original case is preserved.
fn extension(path: &Path, is_directory: bool) -> String {
    if is_directory {
        return String::new();
    }
    match path.extension() {
        Some(value) if !value.is_empty() => format!(".{}", value.to_string_lossy()),
        _ => String::new(),
    }
}

fn display_path(path: &Path) -> String {
    path.to_string_lossy().into_owned()
}

fn error_text(path: &Path, error: &std::io::Error) -> String {
    format!("{}: {}", display_path(path), error)
}

fn root_not_traversed(path: &Path, reason: &str) -> String {
    format!(
        "{}: not traversed ({}); enable link traversal to scan it",
        display_path(path),
        reason
    )
}

#[cfg(unix)]
fn directory_identity(
    _path: &Path,
    metadata: &Metadata,
    _want_canonical: bool,
) -> Option<DirectoryIdentity> {
    use std::os::unix::fs::MetadataExt;
    let inode = metadata.ino();
    (inode != 0).then(|| DirectoryIdentity::Inode(metadata.dev(), inode))
}

#[cfg(windows)]
fn directory_identity(
    path: &Path,
    _metadata: &Metadata,
    want_canonical: bool,
) -> Option<DirectoryIdentity> {
    if !want_canonical {
        return None;
    }
    fs::canonicalize(path)
        .ok()
        .map(|resolved| DirectoryIdentity::Canonical(display_path(&resolved)))
}

#[cfg(not(any(unix, windows)))]
fn directory_identity(
    _path: &Path,
    _metadata: &Metadata,
    _want_canonical: bool,
) -> Option<DirectoryIdentity> {
    None
}

fn enumerate_impl(
    root: &Path,
    cancelled: &AtomicBool,
    follow_links: bool,
    reparse_mounts: bool,
) -> ListingRecord {
    let mut entries = Vec::new();
    let mut scanned = 0usize;
    let mut errors = Vec::new();
    let mut complete = true;
    let mut was_cancelled = false;
    let mut skipped: Vec<SkipRecord> = Vec::new();

    if cancelled.load(Ordering::Relaxed) {
        return (entries, scanned, errors, false, true, skipped);
    }
    let root_metadata = match fs::symlink_metadata(root) {
        Ok(metadata) => metadata,
        Err(error) => {
            return (entries, scanned, vec![error_text(root, &error)], false, false, skipped);
        }
    };
    let root_is_symlink = root_metadata.file_type().is_symlink();
    let root_is_reparse = is_reparse_point(&root_metadata);
    let root_is_link = root_is_symlink || root_is_reparse;
    if !root_metadata.is_dir() && !root_is_link {
        return (
            entries,
            scanned,
            vec![format!("{}: not a directory", display_path(root))],
            false,
            false,
            skipped,
        );
    }
    let root_followed = may_traverse(root_is_symlink, root_is_reparse, follow_links, reparse_mounts);
    if let Some(reason) = skip_reason(root_is_symlink, root_is_reparse, root_followed) {
        return (
            entries,
            scanned,
            vec![root_not_traversed(root, reason)],
            false,
            false,
            skipped,
        );
    }

    let children = match fs::read_dir(root) {
        Ok(children) => children,
        Err(error) => {
            return (entries, scanned, vec![error_text(root, &error)], false, false, skipped);
        }
    };

    for child_result in children {
        if cancelled.load(Ordering::Relaxed) {
            complete = false;
            was_cancelled = true;
            break;
        }
        let child = match child_result {
            Ok(child) => child,
            Err(error) => {
                scanned += 1;
                complete = false;
                errors.push(error_text(root, &error));
                continue;
            }
        };
        scanned += 1;
        let child_path = child.path();
        let name = child.file_name().to_string_lossy().into_owned();
        // DirEntry::metadata does not follow links, so link rows keep link stats.
        let metadata = match child.metadata() {
            Ok(metadata) => metadata,
            Err(error) => {
                complete = false;
                errors.push(error_text(&child_path, &error));
                continue;
            }
        };
        let symlink = metadata.file_type().is_symlink();
        let reparse = is_reparse_point(&metadata);
        let followed = may_traverse(symlink, reparse, follow_links, reparse_mounts);
        let mut is_directory = metadata.is_dir();
        if followed && (symlink || reparse) {
            is_directory = fs::metadata(&child_path).map(|target| target.is_dir()).unwrap_or(false);
        }
        let is_directory = is_directory && (followed || !(symlink || reparse));
        let size = if is_directory { None } else { Some(metadata.len()) };
        if let Some(reason) = skip_reason(symlink, reparse, followed) {
            skipped.push((display_path(&child_path), reason));
        }
        entries.push((
            name,
            is_directory,
            size,
            modified_ns(&metadata),
            extension(&child_path, is_directory),
            symlink,
            reparse,
        ));
    }

    (entries, scanned, errors, complete, was_cancelled, skipped)
}

fn calculate_impl(
    root: &Path,
    cancelled: &AtomicBool,
    follow_links: bool,
    reparse_mounts: bool,
) -> SizeRecord {
    let mut total_size = 0u64;
    let mut total_files = 0u64;
    let mut total_directories = 0u64;
    let mut scanned = 0usize;
    let mut errors = Vec::new();
    let mut complete = true;
    let mut was_cancelled = false;
    let mut skipped: Vec<SkipRecord> = Vec::new();
    let empty = |errors: Vec<String>, skipped: Vec<SkipRecord>| -> SizeRecord {
        (0, 0, 0, 0, errors, false, false, skipped)
    };

    let root_metadata = match fs::symlink_metadata(root) {
        Ok(metadata) => metadata,
        Err(error) => return empty(vec![error_text(root, &error)], skipped),
    };
    let root_is_symlink = root_metadata.file_type().is_symlink();
    let root_is_reparse = is_reparse_point(&root_metadata);
    let root_is_link = root_is_symlink || root_is_reparse;
    if !root_metadata.is_dir() && !root_is_link {
        return empty(vec![format!("{}: not a directory", display_path(root))], skipped);
    }
    let root_followed = may_traverse(root_is_symlink, root_is_reparse, follow_links, reparse_mounts);
    if let Some(reason) = skip_reason(root_is_symlink, root_is_reparse, root_followed) {
        skipped.push((display_path(root), reason));
        return empty(vec![root_not_traversed(root, reason)], skipped);
    }

    let mut pending = vec![root.to_path_buf()];
    let mut visited: HashSet<DirectoryIdentity> = HashSet::new();
    if let Some(identity) = directory_identity(root, &root_metadata, root_is_link || follow_links) {
        visited.insert(identity);
    }

    while let Some(directory) = pending.pop() {
        if cancelled.load(Ordering::Relaxed) {
            complete = false;
            was_cancelled = true;
            break;
        }
        total_directories += 1;
        let children = match fs::read_dir(&directory) {
            Ok(children) => children,
            Err(error) => {
                complete = false;
                errors.push(error_text(&directory, &error));
                continue;
            }
        };

        for child_result in children {
            if cancelled.load(Ordering::Relaxed) {
                complete = false;
                was_cancelled = true;
                break;
            }
            let child = match child_result {
                Ok(child) => child,
                Err(error) => {
                    scanned += 1;
                    complete = false;
                    errors.push(error_text(&directory, &error));
                    continue;
                }
            };
            scanned += 1;
            let child_path = child.path();
            let metadata = match child.metadata() {
                Ok(metadata) => metadata,
                Err(error) => {
                    complete = false;
                    errors.push(error_text(&child_path, &error));
                    continue;
                }
            };
            let symlink = metadata.file_type().is_symlink();
            let reparse = is_reparse_point(&metadata);
            let followed = may_traverse(symlink, reparse, follow_links, reparse_mounts);
            if let Some(reason) = skip_reason(symlink, reparse, followed) {
                skipped.push((display_path(&child_path), reason));
                continue;
            }
            // When the caller opted in to following links, measure the target.
            let stats_for_size = if symlink || reparse {
                fs::metadata(&child_path).unwrap_or_else(|_| metadata.clone())
            } else {
                metadata
            };
            if stats_for_size.is_dir() {
                if let Some(identity) = directory_identity(
                    &child_path,
                    &stats_for_size,
                    symlink || reparse || follow_links,
                ) {
                    if !visited.insert(identity) {
                        skipped.push((display_path(&child_path), SKIP_DUPLICATE));
                        continue;
                    }
                }
                pending.push(child_path);
            } else if stats_for_size.is_file() {
                total_files += 1;
                total_size = total_size.saturating_add(stats_for_size.len());
            } else {
                skipped.push((display_path(&child_path), SKIP_NON_REGULAR));
            }
        }
        if was_cancelled {
            break;
        }
    }

    (
        total_size,
        total_files,
        total_directories,
        scanned,
        errors,
        complete,
        was_cancelled,
        skipped,
    )
}

#[pyfunction]
#[pyo3(signature = (path, token, follow_links=false, reparse_mounts=false))]
fn enumerate_directory(
    py: Python<'_>,
    path: String,
    token: PyRef<'_, CancellationToken>,
    follow_links: bool,
    reparse_mounts: bool,
) -> ListingRecord {
    let cancelled = Arc::clone(&token.cancelled);
    drop(token);
    py.allow_threads(move || {
        enumerate_impl(Path::new(&path), &cancelled, follow_links, reparse_mounts)
    })
}

#[pyfunction]
#[pyo3(signature = (path, token, follow_links=false, reparse_mounts=false))]
fn calculate_size(
    py: Python<'_>,
    path: String,
    token: PyRef<'_, CancellationToken>,
    follow_links: bool,
    reparse_mounts: bool,
) -> SizeRecord {
    let cancelled = Arc::clone(&token.cancelled);
    drop(token);
    py.allow_threads(move || {
        calculate_impl(Path::new(&path), &cancelled, follow_links, reparse_mounts)
    })
}

#[pymodule]
fn _rust_scanner(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_class::<CancellationToken>()?;
    module.add_function(wrap_pyfunction!(enumerate_directory, module)?)?;
    module.add_function(wrap_pyfunction!(calculate_size, module)?)?;
    module.add("ENGINE_NAME", "rust")?;
    module.add("ENGINE_VERSION", env!("CARGO_PKG_VERSION"))?;
    Ok(())
}
