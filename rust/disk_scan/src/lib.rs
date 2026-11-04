use std::collections::HashSet;
use std::fs::{self, Metadata};
use std::path::Path;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::UNIX_EPOCH;

use pyo3::prelude::*;

#[cfg(windows)]
const FILE_ATTRIBUTE_REPARSE_POINT: u32 = 0x0400;

type EntryRecord = (String, bool, Option<u64>, Option<i64>, String, bool, bool);
type ListingRecord = (Vec<EntryRecord>, usize, usize, Vec<String>, bool, bool);
type SizeRecord = (u64, u64, u64, usize, usize, Vec<String>, bool, bool);

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

fn is_link_or_reparse(metadata: &Metadata) -> bool {
    metadata.file_type().is_symlink() || is_reparse_point(metadata)
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

fn extension(path: &Path, is_directory: bool) -> String {
    if is_directory {
        return String::new();
    }
    path.extension()
        .and_then(|value| value.to_str())
        .map(|value| format!(".{}", value.to_lowercase()))
        .unwrap_or_default()
}

fn display_path(path: &Path) -> String {
    path.to_string_lossy().into_owned()
}

fn error_text(path: &Path, error: &std::io::Error) -> String {
    format!("{}: {}", display_path(path), error)
}

fn enumerate_impl(root: &Path, cancelled: &AtomicBool) -> ListingRecord {
    let mut entries = Vec::new();
    let mut scanned = 0usize;
    let mut skipped = 0usize;
    let mut errors = Vec::new();
    let mut complete = true;
    let mut was_cancelled = false;

    if cancelled.load(Ordering::Relaxed) {
        return (entries, scanned, skipped, errors, false, true);
    }
    let root_metadata = match fs::symlink_metadata(root) {
        Ok(metadata) => metadata,
        Err(error) => {
            return (entries, scanned, 1, vec![error_text(root, &error)], false, false);
        }
    };
    if !root_metadata.is_dir() || is_link_or_reparse(&root_metadata) {
        return (
            entries,
            scanned,
            1,
            vec![format!("{}: not a traversable directory", display_path(root))],
            false,
            false,
        );
    }

    let children = match fs::read_dir(root) {
        Ok(children) => children,
        Err(error) => {
            return (entries, scanned, 1, vec![error_text(root, &error)], false, false);
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
                skipped += 1;
                complete = false;
                errors.push(error_text(root, &error));
                continue;
            }
        };
        scanned += 1;
        let child_path = child.path();
        let name = child.file_name().to_string_lossy().into_owned();
        let metadata = match child.metadata() {
            Ok(metadata) => metadata,
            Err(error) => {
                skipped += 1;
                complete = false;
                errors.push(error_text(&child_path, &error));
                continue;
            }
        };
        let symlink = metadata.file_type().is_symlink();
        let reparse = is_reparse_point(&metadata);
        let is_directory = metadata.is_dir() && !(symlink || reparse);
        let size = if is_directory { None } else { Some(metadata.len()) };
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

    (entries, scanned, skipped, errors, complete, was_cancelled)
}

#[cfg(unix)]
fn directory_identity(metadata: &Metadata) -> Option<(u64, u64)> {
    use std::os::unix::fs::MetadataExt;
    let inode = metadata.ino();
    (inode != 0).then_some((metadata.dev(), inode))
}

#[cfg(windows)]
fn directory_identity(metadata: &Metadata) -> Option<(u64, u64)> {
    use std::os::windows::fs::MetadataExt;
    let volume = metadata.volume_serial_number()? as u64;
    let file_index = metadata.file_index()?;
    (file_index != 0).then_some((volume, file_index))
}

#[cfg(not(any(unix, windows)))]
fn directory_identity(_metadata: &Metadata) -> Option<(u64, u64)> {
    None
}

fn calculate_impl(root: &Path, cancelled: &AtomicBool) -> SizeRecord {
    let mut total_size = 0u64;
    let mut total_files = 0u64;
    let mut total_directories = 0u64;
    let mut scanned = 0usize;
    let mut skipped = 0usize;
    let mut errors = Vec::new();
    let mut complete = true;
    let mut was_cancelled = false;

    let root_metadata = match fs::symlink_metadata(root) {
        Ok(metadata) => metadata,
        Err(error) => {
            return (0, 0, 0, 0, 1, vec![error_text(root, &error)], false, false);
        }
    };
    if !root_metadata.is_dir() || is_link_or_reparse(&root_metadata) {
        return (
            0,
            0,
            0,
            0,
            1,
            vec![format!("{}: not a traversable directory", display_path(root))],
            false,
            false,
        );
    }

    let mut pending = vec![root.to_path_buf()];
    let mut visited: HashSet<(u64, u64)> = HashSet::new();
    if let Some(identity) = directory_identity(&root_metadata) {
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
                skipped += 1;
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
                    skipped += 1;
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
                    skipped += 1;
                    complete = false;
                    errors.push(error_text(&child_path, &error));
                    continue;
                }
            };
            if is_link_or_reparse(&metadata) {
                skipped += 1;
                continue;
            }
            if metadata.is_dir() {
                if let Some(identity) = directory_identity(&metadata) {
                    if !visited.insert(identity) {
                        skipped += 1;
                        continue;
                    }
                }
                pending.push(child_path);
            } else if metadata.is_file() {
                total_files += 1;
                total_size = total_size.saturating_add(metadata.len());
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
        skipped,
        errors,
        complete,
        was_cancelled,
    )
}

#[pyfunction]
fn enumerate_directory(
    py: Python<'_>,
    path: String,
    token: PyRef<'_, CancellationToken>,
) -> ListingRecord {
    let cancelled = Arc::clone(&token.cancelled);
    drop(token);
    py.allow_threads(|| enumerate_impl(Path::new(&path), &cancelled))
}

#[pyfunction]
fn calculate_size(
    py: Python<'_>,
    path: String,
    token: PyRef<'_, CancellationToken>,
) -> SizeRecord {
    let cancelled = Arc::clone(&token.cancelled);
    drop(token);
    py.allow_threads(|| calculate_impl(Path::new(&path), &cancelled))
}

#[pymodule]
fn _rust_scanner(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_class::<CancellationToken>()?;
    module.add_function(wrap_pyfunction!(enumerate_directory, module)?)?;
    module.add_function(wrap_pyfunction!(calculate_size, module)?)?;
    module.add("ENGINE_NAME", "rust")?;
    Ok(())
}
