using System;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.IO;
using System.Net.Http;
using System.Net.Http.Headers;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Threading.Tasks;

// ==========================================
// 1. CONFIGURATION LOGIC
// ==========================================
string webuiUrl = Environment.GetEnvironmentVariable("WEBUI_URL") ?? "http://localhost:3000";
string apiKey = Environment.GetEnvironmentVariable("API_KEY");
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

    public MultiRepoKnowledgeSync(string baseUrl, string apiKey, string stateFilePath)
    {
        _baseUrl = baseUrl.TrimEnd('/');
        _stateFilePath = stateFilePath;
        _fileState = LoadState();

        _client = new HttpClient();
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

            string kbId = await GetOrCreateKnowledgeBaseAsync(repoName);
            if (string.IsNullOrEmpty(kbId))
            {
                Console.WriteLine($"Failed to resolve Knowledge Base for {repoName}. Skipping directory.");
                continue;
            }

            await SyncDirectoryAsync(dir, kbId);
        }
    }

    private async Task<string> GetOrCreateKnowledgeBaseAsync(string repoName)
    {
        Console.WriteLine($"Checking if Knowledge Base '{repoName}' exists...");
        var response = await _client.GetAsync($"{_baseUrl}/api/v1/knowledge/");
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
                    string id = element.GetProperty("id").GetString();
                    Console.WriteLine($"Found existing Knowledge Base '{repoName}' (ID: {id}).");
                    return id;
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

        var createResponse = await _client.PostAsync($"{_baseUrl}/api/v1/knowledge/create", content);
        if (createResponse.IsSuccessStatusCode)
        {
            var createJson = await createResponse.Content.ReadAsStringAsync();
            using var createDoc = JsonDocument.Parse(createJson);
            
            string newId = null;
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
                Console.WriteLine($"  [Pending Sync] {Path.GetFileName(file)}");
            }
        });

        if (filesToUpload.IsEmpty)
        {
            Console.WriteLine("No new or modified files detected. Repo is up to date.");
            return;
        }

        Console.WriteLine($"Uploading {filesToUpload.Count} new/modified files...");
        var uploadedFileIds = new List<string>();
        int uploadCount = 0;

        var parallelOptions = new ParallelOptions { MaxDegreeOfParallelism = 4 };
        await Parallel.ForEachAsync(filesToUpload, parallelOptions, async (file, token) =>
        {
            string fileId = await UploadFileAsync(file);
            if (!string.IsNullOrEmpty(fileId))
            {
                lock (uploadedFileIds) 
                { 
                    uploadedFileIds.Add(fileId); 
                    uploadCount++;
                    if (uploadCount % 50 == 0) 
                    {
                        Console.WriteLine($"  ...Uploaded {uploadCount}/{filesToUpload.Count} files...");
                    }
                }
            }
        });

        if (uploadedFileIds.Count > 0)
        {
            Console.WriteLine($"Attaching {uploadedFileIds.Count} successfully uploaded files to Knowledge Base...");
            bool batchSuccess = await AttachToKnowledgeBaseAsync(uploadedFileIds, kbId);
            if (batchSuccess)
            {
                _fileState = updatedState;
                SaveState(_fileState);
                Console.WriteLine("Synchronisation successful.");
            }
            else
            {
                Console.WriteLine("Synchronisation failed during the batch attachment phase.");
            }
        }
        else
        {
            Console.WriteLine("No files were successfully uploaded. Skipping attachment phase.");
        }
    }
    
    private async Task<string> UploadFileAsync(string filePath)
    {
        string fileName = Path.GetFileName(filePath);
        try
        {
            using var content = new MultipartFormDataContent();
            using var fileStream = new FileStream(filePath, FileMode.Open, FileAccess.Read);
            content.Add(new StreamContent(fileStream), "file", fileName);

            var response = await _client.PostAsync($"{_baseUrl}/api/v1/files/", content);
            if (response.IsSuccessStatusCode)
            {
                var json = await response.Content.ReadAsStringAsync();
                using var doc = JsonDocument.Parse(json);
                string id = doc.RootElement.GetProperty("id").GetString();
                Console.WriteLine($"  [Success] Uploaded {fileName} (File ID: {id})");
                return id;
            }
            else
            {
                string error = await response.Content.ReadAsStringAsync();
                Console.WriteLine($"  [Failed] Uploading {fileName}. Status: {response.StatusCode}. Details: {error}");
            }
        }
        catch (Exception ex)
        {
            Console.WriteLine($"  [Error] Exception uploading {fileName}: {ex.Message}");
        }
        return null;
    }

    private async Task<bool> AttachToKnowledgeBaseAsync(List<string> fileIds, string kbId)
    {
        var payload = new List<object>();
        foreach (var id in fileIds) { payload.Add(new { file_id = id }); }

        var content = new StringContent(JsonSerializer.Serialize(payload));
        content.Headers.ContentType = new MediaTypeHeaderValue("application/json");

        var response = await _client.PostAsync($"{_baseUrl}/api/v1/knowledge/{kbId}/files/batch/add", content);
        
        if (!response.IsSuccessStatusCode)
        {
            string errorDetails = await response.Content.ReadAsStringAsync();
            Console.WriteLine($"  [Error] Batch attach failed. Status: {response.StatusCode}. Details: {errorDetails}");
        }
        
        return response.IsSuccessStatusCode;
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