use serde::{Deserialize, Serialize};
use std::future::Future;
use std::io::ErrorKind;
use std::path::Path;
use tokio::fs::OpenOptions;
use tokio::io::{AsyncBufReadExt, AsyncSeekExt, BufReader, SeekFrom};
use tokio::task::JoinHandle;
use tokio::time::{sleep, Duration};

use crate::base::AppError;

#[cfg(unix)]
fn same_file(left: &std::fs::Metadata, right: &std::fs::Metadata) -> bool {
    use std::os::unix::fs::MetadataExt;

    left.dev() == right.dev() && left.ino() == right.ino()
}

#[cfg(not(unix))]
fn same_file(_left: &std::fs::Metadata, _right: &std::fs::Metadata) -> bool {
    true
}

#[derive(Debug, Serialize, Deserialize)]
pub enum FileMonitorEvent {
    NewFile,
    NewLine(String),
}

pub struct FileMonitor {
    task_holder: Option<JoinHandle<()>>,
}

impl Default for FileMonitor {
    fn default() -> Self {
        Self::new()
    }
}

impl FileMonitor {
    pub fn new() -> Self {
        Self { task_holder: None }
    }

    pub async fn start<F, Fut>(&mut self, file_path: &str, on_update: F)
    where
        F: Fn(FileMonitorEvent) -> Fut + Send + Sync + 'static,
        Fut: Future<Output = Result<(), AppError>> + Send + 'static,
    {
        let file_path_clone = file_path.to_string();

        let monitor = tokio::spawn(async move {
            if let Err(error) = Self::start_monitor(file_path_clone.as_str(), on_update).await {
                eprintln!("File monitor failed for {file_path_clone}: {error}");
            }
        });

        if let Some(old_task) = self.task_holder.replace(monitor) {
            println!("Aborting old file monitor task");
            old_task.abort();
        }
    }

    pub async fn stop(&mut self) {
        if let Some(handle) = self.task_holder.take() {
            handle.abort();
        }
    }

    async fn start_monitor<F, Fut>(file_path: &str, on_update: F) -> Result<(), AppError>
    where
        F: Fn(FileMonitorEvent) -> Fut + Send + Sync + 'static,
        Fut: Future<Output = Result<(), AppError>> + Send + 'static,
    {
        while !Path::new(file_path).exists() {
            sleep(Duration::from_millis(10)).await;
        }

        let file = OpenOptions::new().read(true).open(file_path).await?;
        let mut reader = BufReader::new(file);

        let metadata = reader.get_ref().metadata().await.unwrap();
        let mut position = metadata.len();

        loop {
            let path_metadata = match tokio::fs::metadata(file_path).await {
                Ok(metadata) => metadata,
                Err(error) if error.kind() == ErrorKind::NotFound => {
                    sleep(Duration::from_millis(10)).await;
                    continue;
                }
                Err(error) => return Err(error.into()),
            };
            let metadata = reader.get_ref().metadata().await?;

            if !same_file(&metadata, &path_metadata) {
                let file = OpenOptions::new().read(true).open(file_path).await?;
                reader = BufReader::new(file);
                position = 0;
                let _ = on_update(FileMonitorEvent::NewFile).await;
            }

            let current_size = reader.get_ref().metadata().await?.len();
            if current_size < position {
                position = 0;
                let _ = on_update(FileMonitorEvent::NewFile).await;
            }

            if reader.stream_position().await? != position {
                reader.seek(SeekFrom::Start(position)).await?;
            }

            let mut line = String::new();

            while let Ok(bytes_read) = reader.read_line(&mut line).await {
                if bytes_read == 0 {
                    break;
                }

                let trimmed_line = line.trim();
                if !trimmed_line.is_empty() {
                    let _ = on_update(FileMonitorEvent::NewLine(trimmed_line.to_string())).await;
                }

                position = reader.stream_position().await?;
                line.clear();
            }

            sleep(Duration::from_millis(10)).await;
        }
    }
}
