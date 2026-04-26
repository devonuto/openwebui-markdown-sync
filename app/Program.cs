using System;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.IO;
using System.Net;
using System.Net.Http;
using System.Net.Http.Headers;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Runtime.CompilerServices;
using System.Threading.Tasks;

[assembly: InternalsVisibleTo("app.Tests")]

// ==========================================
// 1. CONFIGURATION LOGIC
// ==========================================
string webuiUrl = Environment.GetEnvironmentVariable("WEBUI_URL") ?? "http://localhost:3000";
string? apiKey = Environment.GetEnvironmentVariable("API_KEY");
string stateFile = Environment.GetEnvironmentVariable("STATE_FILE") ?? "/data/sync_state.json";
string rootReposPath = "/markdown-repos";
string logsPath = "/logs";
const int logRetentionDays = 14;
string logFilePath = Path.Combine(logsPath, $"{DateTime.Now:yyyy-MM-dd}_Container.log");

Directory.CreateDirectory(logsPath);

using var logWriter = new FileStreamTextWriter(logFilePath, Console.Out);
Console.SetOut(logWriter);
Console.SetError(logWriter);

Console.WriteLine("=== Starting Markdown Knowledge Sync ===");
Console.WriteLine($"WebUI URL:       {webuiUrl}");
Console.WriteLine($"State File:      {stateFile}");
Console.WriteLine($"Root Repos Path: {rootReposPath}");
Console.WriteLine($"Log File:        {logFilePath}");
Console.WriteLine("========================================");

if (string.IsNullOrEmpty(apiKey))
{
    Console.WriteLine("CRITICAL: API_KEY environment variable is missing.");
    return;
}

try
{
    var syncTool = new MultiRepoKnowledgeSync(webuiUrl, apiKey, stateFile);
    await syncTool.SyncAllRepositoriesAsync(rootReposPath);
    Console.WriteLine("\n=== Sync Process Complete ===");
}
finally
{
    RuntimeHelpers.CleanupOldLogFiles(logsPath, logRetentionDays, logFilePath);
    Console.Out.Flush();
    Console.Error.Flush();
}

// ==========================================
// 2. SYNCHRONISATION LOGIC
// ==========================================
public class MultiRepoKnowledgeSync
{
    private readonly HttpClient _client;
    private readonly string _baseUrl;
    private readonly string _stateFilePath;
    private Dictionary<string, string> _fileState;
    private static readonly TimeSpan RequestTimeout = TimeSpan.FromMinutes(10);
    private static readonly TimeSpan DefaultRetryDelay = TimeSpan.FromMinutes(1);
    private const int DefaultMaxRetries = 5;
    private readonly TimeSpan _retryDelay;
    private readonly int _maxRetries;

    public MultiRepoKnowledgeSync(string baseUrl, string apiKey, string stateFilePath)
    {
        _baseUrl = baseUrl.TrimEnd('/');
        _stateFilePath = stateFilePath;
        _fileState = LoadState();
        _retryDelay = GetRetryDelayFromEnvironment();
        _maxRetries = GetMaxRetriesFromEnvironment();

        _client = new HttpClient(new SocketsHttpHandler
        {
            PooledConnectionLifetime = TimeSpan.FromMinutes(2)
        })
        { Timeout = RequestTimeout };
        _client.DefaultRequestHeaders.Authorization = new AuthenticationHeaderValue("Bearer", apiKey);
    }

    internal MultiRepoKnowledgeSync(string baseUrl, string apiKey, string stateFilePath, HttpClient httpClient)
        : this(baseUrl, apiKey, stateFilePath, httpClient, DefaultRetryDelay, DefaultMaxRetries)
    {
    }

    internal MultiRepoKnowledgeSync(string baseUrl, string apiKey, string stateFilePath, HttpClient httpClient, TimeSpan retryDelay, int maxRetries)
    {
        _baseUrl = baseUrl.TrimEnd('/');
        _stateFilePath = stateFilePath;
        _fileState = LoadState();
        _retryDelay = retryDelay;
        _maxRetries = maxRetries;
        _client = httpClient;
        _client.DefaultRequestHeaders.Authorization = new AuthenticationHeaderValue("Bearer", apiKey);
    }

    public async Task SyncAllRepositoriesAsync(string rootReposPath)
    {
        if (!Directory.Exists(rootReposPath))
        {
            Console.WriteLine($"CRITICAL: The directory '{rootReposPath}' does not exist.");
            return;
        }

        var directories = Directory.GetDirectories(rootReposPath);
        Console.WriteLine($"Found {directories.Length} subdirectories in the root path.");

        foreach (var dir in directories)
        {
            string repoName = new DirectoryInfo(dir).Name;

            // Ignore Git metadata and other hidden folders
            if (repoName.StartsWith(".")) 
            {
                Console.WriteLine($"Skipping hidden directory: {repoName}");
                continue;
            }

            Console.WriteLine($"\n--- Processing repository: {repoName} ---");

            string? kbId = await GetOrCreateKnowledgeBaseAsync(repoName);
            if (string.IsNullOrEmpty(kbId))
            {
                Console.WriteLine($"Failed to resolve Knowledge Base for {repoName}. Skipping directory.");
                continue;
            }

            await SyncDirectoryAsync(dir, kbId);
        }
    }

    private async Task<string?> GetOrCreateKnowledgeBaseAsync(string repoName)
    {
        Console.WriteLine($"Checking if Knowledge Base '{repoName}' exists...");
        using var response = await ExecuteWithRetryAsync(
            () => _client.GetAsync($"{_baseUrl}/api/v1/knowledge/"),
            "fetch knowledge bases",
            repoName);

        if (response is null)
        {
            Console.WriteLine($"Warning: Failed to fetch knowledge bases for '{repoName}' after retries.");
            return null;
        }

        if (response.IsSuccessStatusCode)
        {
            var json = await response.Content.ReadAsStringAsync();
            using var doc = JsonDocument.Parse(json);
            
            JsonElement elementsToSearch;

            // Handle flexible API structures
            if (doc.RootElement.ValueKind == JsonValueKind.Array)
            {
                elementsToSearch = doc.RootElement;
            }
            else if (doc.RootElement.ValueKind == JsonValueKind.Object)
            {
                if (doc.RootElement.TryGetProperty("items", out var itemsProp) && itemsProp.ValueKind == JsonValueKind.Array)
                {
                    elementsToSearch = itemsProp;
                }
                else if (doc.RootElement.TryGetProperty("data", out var dataProp) && dataProp.ValueKind == JsonValueKind.Array)
                {
                    elementsToSearch = dataProp;
                }
                else
                {
                    Console.WriteLine($"Unexpected API response structure. Raw JSON: {json}");
                    return null;
                }
            }
            else
            {
                Console.WriteLine($"Unexpected API response structure. Raw JSON: {json}");
                return null;
            }
            
            foreach (var element in elementsToSearch.EnumerateArray())
            {
                if (element.TryGetProperty("name", out var nameProp) && nameProp.GetString() == repoName)
                {
                    string? id = element.GetProperty("id").GetString();
                    if (!string.IsNullOrEmpty(id))
                    {
                        Console.WriteLine($"Found existing Knowledge Base '{repoName}' (ID: {id}).");
                        return id;
                    }
                }
            }
        }
        else
        {
            Console.WriteLine($"Warning: Failed to fetch knowledge bases. Status code: {response.StatusCode}");
        }

        Console.WriteLine($"Knowledge Base '{repoName}' not found. Creating...");
        var payload = new { name = repoName, description = $"Auto-synced repository: {repoName}" };
        var content = new StringContent(JsonSerializer.Serialize(payload));
        content.Headers.ContentType = new MediaTypeHeaderValue("application/json");

        using var createResponse = await ExecuteWithRetryAsync(
            () => _client.PostAsync($"{_baseUrl}/api/v1/knowledge/create", content),
            "create knowledge base",
            repoName);

        if (createResponse is null)
        {
            Console.WriteLine($"Error creating Knowledge Base '{repoName}': no response after retries.");
            return null;
        }

        if (createResponse.IsSuccessStatusCode)
        {
            var createJson = await createResponse.Content.ReadAsStringAsync();
            using var createDoc = JsonDocument.Parse(createJson);
            
            string? newId = null;
            if (createDoc.RootElement.TryGetProperty("id", out var idProp))
            {
                newId = idProp.GetString();
            }
            else if (createDoc.RootElement.TryGetProperty("data", out var dataProp) && dataProp.TryGetProperty("id", out var dataIdProp))
            {
                newId = dataIdProp.GetString();
            }

            if (!string.IsNullOrEmpty(newId))
            {
                Console.WriteLine($"Successfully created Knowledge Base '{repoName}' (ID: {newId}).");
                return newId;
            }
        }

        Console.WriteLine($"Error creating Knowledge Base: {await createResponse.Content.ReadAsStringAsync()}");
        return null;
    }

    private async Task SyncDirectoryAsync(string directoryPath, string kbId)
    {
        var options = new EnumerationOptions
        {
            RecurseSubdirectories = true,
            MatchCasing = MatchCasing.CaseInsensitive,
            IgnoreInaccessible = true
        };

        var markdownFiles = Directory.GetFiles(directoryPath, "*.md", options);
        var filesToUpload = new ConcurrentBag<string>();
        var updatedState = new Dictionary<string, string>(_fileState);

        Console.WriteLine($"Scanning {markdownFiles.Length} Markdown files in '{new DirectoryInfo(directoryPath).Name}'...");

        Parallel.ForEach(markdownFiles, file =>
        {
            string hash = ComputeFileHash(file);
            bool isNewOrModified = !_fileState.ContainsKey(file) || _fileState[file] != hash;

            if (isNewOrModified)
            {
                filesToUpload.Add(file);
                lock (updatedState) { updatedState[file] = hash; }
            }
        });

        if (filesToUpload.IsEmpty)
        {
            Console.WriteLine("No new or modified files detected. Repo is up to date.");
            return;
        }

        var filesToUploadList = filesToUpload.ToList();
        int totalFiles = filesToUploadList.Count;
        int totalAttached = 0;
        int totalProcessed = 0;

        Console.WriteLine($"Processing {totalFiles} new/modified files one at a time...");

        foreach (var filePath in filesToUploadList)
        {
            totalProcessed++;
            string? fileId = await UploadFileAsync(filePath, directoryPath);
            if (string.IsNullOrEmpty(fileId))
            {
                Console.WriteLine($"  [Progress] {totalProcessed}/{totalFiles} processed. Upload failed, skipping attach.");
                continue;
            }

            bool attachSuccess = await TryAttachSingleFileAsync(fileId, kbId);
            if (!attachSuccess)
            {
                Console.WriteLine($"  [Failed] Attach failed for file ID {fileId}. State not saved for this file.");
                Console.WriteLine($"  [Progress] {totalProcessed}/{totalFiles} processed. Total attached: {totalAttached}/{totalFiles}");
                continue;
            }

            if (updatedState.TryGetValue(filePath, out var hash))
            {
                _fileState[filePath] = hash;
            }

            SaveState(_fileState);
            totalAttached++;
            Console.WriteLine($"  [Success] Attached and saved 1 file. Progress: {totalProcessed}/{totalFiles}. Total attached: {totalAttached}/{totalFiles}");
        }

        Console.WriteLine($"Synchronisation complete with {totalAttached}/{totalFiles} files attached.");
    }
    
    private async Task<string?> UploadFileAsync(string filePath, string repoRootPath)
    {
        string relativePath = Path.GetRelativePath(repoRootPath, filePath);
        try
        {
            using var response = await ExecuteWithRetryAsync(
                async () =>
                {
                    using var content = new MultipartFormDataContent();
                    using var fileStream = new FileStream(filePath, FileMode.Open, FileAccess.Read, FileShare.Read);
                    content.Add(new StreamContent(fileStream), "file", Path.GetFileName(filePath));
                    return await _client.PostAsync($"{_baseUrl}/api/v1/files/", content);
                },
                "upload file",
                relativePath);

            if (response is null)
            {
                Console.WriteLine($"  [Failed] Uploading {relativePath}. No response after retries.");
                return null;
            }

            if (response.IsSuccessStatusCode)
            {
                var json = await response.Content.ReadAsStringAsync();
                using var doc = JsonDocument.Parse(json);
                string? id = ParseFileId(doc.RootElement);

                if (string.IsNullOrEmpty(id))
                {
                    Console.WriteLine($"  [Failed] Uploading {relativePath}. Upload succeeded but no file ID found.");
                    return null;
                }

                Console.WriteLine($"  [Success] Uploaded {relativePath} (File ID: {id})");
                return id;
            }
            else
            {
                string error = await response.Content.ReadAsStringAsync();
                Console.WriteLine($"  [Failed] Uploading {relativePath}. Status: {response.StatusCode}. Details: {error}");
            }
        }
        catch (Exception ex)
        {
            Console.WriteLine($"  [Error] Exception uploading {relativePath}: {ex.Message}");
        }
        return null;
    }

    private async Task<bool> TryAttachSingleFileAsync(string fileId, string kbId)
    {
        using var response = await ExecuteWithRetryAsync(
            () => SendSingleAttachRequestAsync(fileId, kbId),
            "attach file",
            $"KB {kbId} (file {fileId})");

        if (response is null)
        {
            Console.WriteLine($"  [Error] Per-file attach failed for file {fileId}. No response after retries.");
            return false;
        }

        if (response.IsSuccessStatusCode)
        {
            return true;
        }

        string primaryErrorDetails = await response.Content.ReadAsStringAsync();
        if (!ShouldAttemptAlternateAttach(response.StatusCode, primaryErrorDetails))
        {
            Console.WriteLine($"  [Error] Per-file attach failed for file {fileId}. Status: {response.StatusCode}. Details: {primaryErrorDetails}");
            return false;
        }

        Console.WriteLine($"  [Info] Retrying attach with alternate endpoint for file {fileId}.");
        using var fallbackResponse = await ExecuteWithRetryAsync(
            () => SendBatchAttachRequestAsync(fileId, kbId),
            "attach file (fallback endpoint)",
            $"KB {kbId} (file {fileId})");

        if (fallbackResponse is null)
        {
            Console.WriteLine($"  [Error] Fallback attach failed for file {fileId}. No response after retries.");
            return false;
        }

        if (!fallbackResponse.IsSuccessStatusCode)
        {
            string fallbackErrorDetails = await fallbackResponse.Content.ReadAsStringAsync();
            Console.WriteLine($"  [Error] Fallback attach failed for file {fileId}. Status: {fallbackResponse.StatusCode}. Details: {fallbackErrorDetails}");
            return false;
        }

        Console.WriteLine($"  [Success] Fallback attach succeeded for file {fileId}.");

        return true;
    }

    private Task<HttpResponseMessage> SendSingleAttachRequestAsync(string fileId, string kbId)
    {
        var requestContent = CreateSingleAttachContent(fileId);
        return _client.PostAsync($"{_baseUrl}/api/v1/knowledge/{kbId}/file/add", requestContent);
    }

    private Task<HttpResponseMessage> SendBatchAttachRequestAsync(string fileId, string kbId)
    {
        var requestContent = CreateBatchAttachContent(fileId);
        return _client.PostAsync($"{_baseUrl}/api/v1/knowledge/{kbId}/files/add", requestContent);
    }

    private static StringContent CreateSingleAttachContent(string fileId)
    {
        var payload = new { file_id = fileId };
        var content = new StringContent(JsonSerializer.Serialize(payload));
        content.Headers.ContentType = new MediaTypeHeaderValue("application/json");
        return content;
    }

    private static StringContent CreateBatchAttachContent(string fileId)
    {
        var payload = new { file_ids = new[] { fileId } };
        var content = new StringContent(JsonSerializer.Serialize(payload));
        content.Headers.ContentType = new MediaTypeHeaderValue("application/json");
        return content;
    }


    private string ComputeFileHash(string filePath)
    {
        using var sha256 = SHA256.Create();
        using var stream = File.OpenRead(filePath);
        var hashBytes = sha256.ComputeHash(stream);
        return BitConverter.ToString(hashBytes).Replace("-", "").ToLowerInvariant();
    }

    private Dictionary<string, string> LoadState()
    {
        if (!File.Exists(_stateFilePath))
        {
            Console.WriteLine($"State file not found at '{_stateFilePath}'. A new one will be created upon successful sync.");
            return new Dictionary<string, string>();
        }
        
        var json = File.ReadAllText(_stateFilePath);
        var state = JsonSerializer.Deserialize<Dictionary<string, string>>(json) ?? new Dictionary<string, string>();
        Console.WriteLine($"Loaded previous sync state ({state.Count} files tracked).");
        return state;
    }

    private void SaveState(Dictionary<string, string> state)
    {
        string? stateDirectory = Path.GetDirectoryName(_stateFilePath);
        if (!string.IsNullOrEmpty(stateDirectory))
        {
            Directory.CreateDirectory(stateDirectory);
        }

        var json = JsonSerializer.Serialize(state, new JsonSerializerOptions { WriteIndented = true });
        File.WriteAllText(_stateFilePath, json);
        Console.WriteLine("State file updated successfully.");
    }

    private static bool ShouldRetry(HttpStatusCode statusCode, string responseBody, string operationName)
    {
        if (statusCode == HttpStatusCode.BadRequest && IsTransientBadRequest(responseBody, operationName))
        {
            return true;
        }

        return statusCode == HttpStatusCode.Unauthorized
            || statusCode == HttpStatusCode.RequestTimeout
            || statusCode == HttpStatusCode.TooManyRequests
            || (int)statusCode >= 500;
    }

    private static bool IsTransientBadRequest(string responseBody, string operationName)
    {
        if (string.IsNullOrWhiteSpace(responseBody))
        {
            return false;
        }

        if (!operationName.Contains("upload", StringComparison.OrdinalIgnoreCase)
            && !operationName.Contains("attach", StringComparison.OrdinalIgnoreCase))
        {
            return false;
        }

        return responseBody.Contains("error uploading file", StringComparison.OrdinalIgnoreCase)
            || responseBody.Contains("temporarily", StringComparison.OrdinalIgnoreCase)
            || responseBody.Contains("timeout", StringComparison.OrdinalIgnoreCase)
            || responseBody.Contains("try again", StringComparison.OrdinalIgnoreCase)
            || responseBody.Contains("connection", StringComparison.OrdinalIgnoreCase)
            || responseBody.Contains("failed to process", StringComparison.OrdinalIgnoreCase);
    }

    private static bool ShouldAttemptAlternateAttach(HttpStatusCode statusCode, string responseBody)
    {
        if (statusCode == HttpStatusCode.NotFound || statusCode == HttpStatusCode.MethodNotAllowed)
        {
            return true;
        }

        if (statusCode != HttpStatusCode.BadRequest || string.IsNullOrWhiteSpace(responseBody))
        {
            return false;
        }

        return responseBody.Contains("file_ids", StringComparison.OrdinalIgnoreCase)
            || responseBody.Contains("files/add", StringComparison.OrdinalIgnoreCase)
            || responseBody.Contains("invalid payload", StringComparison.OrdinalIgnoreCase)
            || responseBody.Contains("missing", StringComparison.OrdinalIgnoreCase);
    }

    private static TimeSpan GetRetryDelayFromEnvironment()
    {
        var raw = Environment.GetEnvironmentVariable("RETRY_DELAY_SECONDS");
        if (int.TryParse(raw, out int seconds) && seconds > 0)
        {
            return TimeSpan.FromSeconds(seconds);
        }

        return DefaultRetryDelay;
    }

    private static int GetMaxRetriesFromEnvironment()
    {
        var raw = Environment.GetEnvironmentVariable("MAX_RETRIES");
        if (int.TryParse(raw, out int retries) && retries > 0)
        {
            return retries;
        }

        return DefaultMaxRetries;
    }

    private async Task<HttpResponseMessage?> ExecuteWithRetryAsync(
        Func<Task<HttpResponseMessage>> operation,
        string operationName,
        string target)
    {
        for (int attempt = 1; attempt <= _maxRetries; attempt++)
        {
            try
            {
                var response = await operation();
                if (response.IsSuccessStatusCode)
                {
                    return response;
                }

                string body = await response.Content.ReadAsStringAsync();
                bool shouldRetry = ShouldRetry(response.StatusCode, body, operationName);

                if (!shouldRetry)
                {
                    Console.WriteLine($"  [No Retry] {operationName} failed for '{target}'. Status: {response.StatusCode}. Classified as non-retryable. Details: {body}");
                    return response;
                }

                if (attempt == _maxRetries)
                {
                    return response;
                }

                Console.WriteLine($"  [Retry {attempt}/{_maxRetries}] {operationName} failed for '{target}'. Status: {response.StatusCode}. Retrying in {(int)_retryDelay.TotalSeconds}s. Details: {body}");
                response.Dispose();
            }
            catch (TaskCanceledException ex)
            {
                if (attempt == _maxRetries)
                {
                    Console.WriteLine($"  [Failed] {operationName} timed out for '{target}' after {_maxRetries} attempts. {ex.Message}");
                    return null;
                }

                Console.WriteLine($"  [Retry {attempt}/{_maxRetries}] {operationName} timed out for '{target}'. Retrying in {(int)_retryDelay.TotalSeconds}s.");
            }
            catch (HttpRequestException ex)
            {
                if (attempt == _maxRetries)
                {
                    Console.WriteLine($"  [Failed] {operationName} request error for '{target}' after {_maxRetries} attempts. {ex.Message}");
                    return null;
                }

                Console.WriteLine($"  [Retry {attempt}/{_maxRetries}] {operationName} request error for '{target}'. Retrying in {(int)_retryDelay.TotalSeconds}s. {ex.Message}");
            }

            await Task.Delay(_retryDelay);
        }

        return null;
    }


    private static string? ParseFileId(JsonElement root)
    {
        if (root.TryGetProperty("id", out var idProp))
        {
            return idProp.GetString();
        }

        if (root.TryGetProperty("data", out var dataProp))
        {
            if (dataProp.ValueKind == JsonValueKind.Object && dataProp.TryGetProperty("id", out var dataIdProp))
            {
                return dataIdProp.GetString();
            }
        }

        return null;
    }
}

public static class RuntimeHelpers
{
    public static void CleanupOldLogFiles(string logsPath, int retentionDays, string currentLogFilePath)
    {
        if (!Directory.Exists(logsPath))
        {
            return;
        }

        DateTime cutoffDate = DateTime.Today.AddDays(-(retentionDays - 1));
        foreach (string filePath in Directory.GetFiles(logsPath, "*_Container.log"))
        {
            if (string.Equals(filePath, currentLogFilePath, StringComparison.OrdinalIgnoreCase))
            {
                continue;
            }

            string fileName = Path.GetFileName(filePath);
            if (!DateTime.TryParseExact(
                    fileName,
                    "yyyy-MM-dd'_Container.log'",
                    null,
                    System.Globalization.DateTimeStyles.None,
                    out DateTime logDate))
            {
                continue;
            }

            if (logDate.Date < cutoffDate)
            {
                File.Delete(filePath);
                Console.WriteLine($"Deleted expired log file: {fileName}");
            }
        }
    }
}

public sealed class FileStreamTextWriter : TextWriter
{
    private readonly TextWriter _consoleWriter;
    private readonly StreamWriter _fileWriter;
    private readonly object _lock = new();

    public FileStreamTextWriter(string filePath, TextWriter consoleWriter)
    {
        _consoleWriter = consoleWriter;
        _fileWriter = new StreamWriter(new FileStream(filePath, FileMode.Append, FileAccess.Write, FileShare.Read))
        {
            AutoFlush = true
        };
    }

    public override Encoding Encoding => Encoding.UTF8;

    public override void Write(char value)
    {
        lock (_lock)
        {
            _consoleWriter.Write(value);
            _fileWriter.Write(value);
        }
    }

    public override void Write(string? value)
    {
        lock (_lock)
        {
            _consoleWriter.Write(value);
            _fileWriter.Write(value);
        }
    }

    public override void WriteLine(string? value)
    {
        lock (_lock)
        {
            _consoleWriter.WriteLine(value);
            _fileWriter.WriteLine(value);
        }
    }

    public override void Flush()
    {
        lock (_lock)
        {
            _consoleWriter.Flush();
            _fileWriter.Flush();
        }
    }

    protected override void Dispose(bool disposing)
    {
        if (!disposing)
        {
            base.Dispose(disposing);
            return;
        }

        lock (_lock)
        {
            _fileWriter.Dispose();
        }

        base.Dispose(disposing);
    }
}