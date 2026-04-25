using System;
using System.Collections.Generic;
using System.IO;
using System.Net;
using System.Net.Http;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Threading;
using System.Threading.Tasks;
using Xunit;

namespace app.Tests;

public sealed class MultiRepoKnowledgeSyncTests : IDisposable
{
    private readonly string _tempRoot;
    private readonly string _stateFile;

    public MultiRepoKnowledgeSyncTests()
    {
        _tempRoot = Path.Combine(Path.GetTempPath(), Path.GetRandomFileName());
        Directory.CreateDirectory(_tempRoot);
        _stateFile = Path.Combine(_tempRoot, "state.json");
    }

    public void Dispose() => Directory.Delete(_tempRoot, recursive: true);

    // -------------------------------------------------------------------------
    // Helpers
    // -------------------------------------------------------------------------

    private MultiRepoKnowledgeSync CreateSync(Func<HttpRequestMessage, HttpResponseMessage> handler,
        out FakeHttpMessageHandler fake)
    {
        fake = new FakeHttpMessageHandler(handler);
        var client = new HttpClient(fake);
        return new MultiRepoKnowledgeSync("http://test", "test-key", _stateFile, client);
    }

    private static HttpResponseMessage JsonOk(string json) =>
        new(HttpStatusCode.OK)
        {
            Content = new StringContent(json, Encoding.UTF8, "application/json")
        };

    private static string ComputeHash(string filePath)
    {
        using var sha256 = SHA256.Create();
        using var stream = File.OpenRead(filePath);
        return BitConverter.ToString(sha256.ComputeHash(stream)).Replace("-", "").ToLowerInvariant();
    }

    // -------------------------------------------------------------------------
    // SyncAllRepositoriesAsync — structural / routing tests
    // -------------------------------------------------------------------------

    [Fact]
    public async Task SyncAllRepositoriesAsync_NonExistentPath_MakesNoHttpCalls()
    {
        var sync = CreateSync(_ => new HttpResponseMessage(HttpStatusCode.OK), out var fake);

        await sync.SyncAllRepositoriesAsync(Path.Combine(_tempRoot, "nonexistent"));

        Assert.Empty(fake.RequestLog);
    }

    [Fact]
    public async Task SyncAllRepositoriesAsync_OnlyHiddenDirectories_MakesNoHttpCalls()
    {
        var reposRoot = Path.Combine(_tempRoot, "repos");
        Directory.CreateDirectory(Path.Combine(reposRoot, ".git"));
        Directory.CreateDirectory(Path.Combine(reposRoot, ".hidden"));

        var sync = CreateSync(_ => new HttpResponseMessage(HttpStatusCode.OK), out var fake);

        await sync.SyncAllRepositoriesAsync(reposRoot);

        Assert.Empty(fake.RequestLog);
    }

    [Fact]
    public async Task SyncAllRepositoriesAsync_MixedDirectories_SkipsHiddenOnly()
    {
        var reposRoot = Path.Combine(_tempRoot, "repos");
        Directory.CreateDirectory(Path.Combine(reposRoot, ".git"));
        Directory.CreateDirectory(Path.Combine(reposRoot, "myrepo")); // no .md files inside

        var sync = CreateSync(req =>
        {
            // KB lookup for "myrepo" — return a match so SyncDirectoryAsync is entered
            if (req.Method == HttpMethod.Get)
                return JsonOk("""[{"id":"kb-1","name":"myrepo"}]""");
            return new HttpResponseMessage(HttpStatusCode.NotFound);
        }, out var fake);

        await sync.SyncAllRepositoriesAsync(reposRoot);

        // Exactly one HTTP call — the GET for KB lookup of the visible "myrepo" directory.
        // No calls should relate to ".git".
        Assert.Single(fake.RequestLog);
        Assert.All(fake.RequestLog, r => Assert.DoesNotContain(".git", r.Url));
    }

    // -------------------------------------------------------------------------
    // SyncDirectoryAsync — no files / already up-to-date
    // -------------------------------------------------------------------------

    [Fact]
    public async Task SyncDirectory_NoMarkdownFiles_MakesOnlyKbLookup()
    {
        var reposRoot = Path.Combine(_tempRoot, "repos");
        var repoDir = Path.Combine(reposRoot, "myrepo");
        Directory.CreateDirectory(repoDir);
        // Add a non-.md file — should be ignored.
        File.WriteAllText(Path.Combine(repoDir, "readme.txt"), "text only");

        var sync = CreateSync(req =>
        {
            if (req.Method == HttpMethod.Get)
                return JsonOk("""[{"id":"kb-1","name":"myrepo"}]""");
            return new HttpResponseMessage(HttpStatusCode.NotFound);
        }, out var fake);

        await sync.SyncAllRepositoriesAsync(reposRoot);

        Assert.Single(fake.RequestLog);
        Assert.Equal("GET", fake.RequestLog.First().Method);
    }

    [Fact]
    public async Task SyncDirectory_AllFilesAlreadySynced_MakesOnlyKbLookup()
    {
        var reposRoot = Path.Combine(_tempRoot, "repos");
        var repoDir = Path.Combine(reposRoot, "myrepo");
        Directory.CreateDirectory(repoDir);

        var filePath = Path.Combine(repoDir, "doc.md");
        File.WriteAllText(filePath, "# Hello");

        // Pre-populate state with the correct hash so the file is considered unchanged.
        var preState = new Dictionary<string, string> { { filePath, ComputeHash(filePath) } };
        File.WriteAllText(_stateFile, JsonSerializer.Serialize(preState));

        var sync = CreateSync(req =>
        {
            if (req.Method == HttpMethod.Get)
                return JsonOk("""[{"id":"kb-1","name":"myrepo"}]""");
            return new HttpResponseMessage(HttpStatusCode.NotFound);
        }, out var fake);

        await sync.SyncAllRepositoriesAsync(reposRoot);

        // Only the KB GET should have been issued; no upload or attach calls.
        Assert.Single(fake.RequestLog);
        Assert.Equal("GET", fake.RequestLog.First().Method);
    }

    // -------------------------------------------------------------------------
    // SyncDirectoryAsync — happy path: upload + single-file attach + state saved
    // -------------------------------------------------------------------------

    [Fact]
    public async Task SyncDirectory_NewFiles_UploadsAttachesAndSavesState()
    {
        var reposRoot = Path.Combine(_tempRoot, "repos");
        var repoDir = Path.Combine(reposRoot, "myrepo");
        Directory.CreateDirectory(repoDir);
        File.WriteAllText(Path.Combine(repoDir, "doc1.md"), "# Doc 1");
        File.WriteAllText(Path.Combine(repoDir, "doc2.md"), "# Doc 2");

        int uploadCounter = 0;
        var sync = CreateSync(req =>
        {
            var url = req.RequestUri!.ToString();
            if (req.Method == HttpMethod.Get && url.Contains("/api/v1/knowledge/"))
                return JsonOk("""[{"id":"kb-1","name":"myrepo"}]""");
            if (req.Method == HttpMethod.Post && url.EndsWith("/api/v1/files/"))
            {
                int n = Interlocked.Increment(ref uploadCounter);
                return JsonOk($$"""{"id":"file-{{n:000}}"}""");
            }
            if (req.Method == HttpMethod.Post && url.Contains("/file/add"))
                return JsonOk("{}");
            return new HttpResponseMessage(HttpStatusCode.NotFound);
        }, out var fake);

        await sync.SyncAllRepositoriesAsync(reposRoot);

        // 1 KB GET + 2 file POSTs + 2 single-file attach POSTs = 5 requests.
        Assert.Equal(5, fake.RequestLog.Count);

        // State file must exist and contain entries for both files.
        Assert.True(File.Exists(_stateFile));
        var state = JsonSerializer.Deserialize<Dictionary<string, string>>(File.ReadAllText(_stateFile));
        Assert.Equal(2, state!.Count);
    }

    [Fact]
    public async Task SyncDirectory_NewFiles_StateContainsCorrectHashes()
    {
        var reposRoot = Path.Combine(_tempRoot, "repos");
        var repoDir = Path.Combine(reposRoot, "myrepo");
        Directory.CreateDirectory(repoDir);
        var filePath = Path.Combine(repoDir, "doc.md");
        File.WriteAllText(filePath, "# Only Doc");

        var sync = CreateSync(req =>
        {
            var url = req.RequestUri!.ToString();
            if (req.Method == HttpMethod.Get)
                return JsonOk("""[{"id":"kb-1","name":"myrepo"}]""");
            if (req.Method == HttpMethod.Post && url.EndsWith("/api/v1/files/"))
                return JsonOk("""{"id":"file-001"}""");
            if (req.Method == HttpMethod.Post && url.Contains("/file/add"))
                return JsonOk("{}");
            return new HttpResponseMessage(HttpStatusCode.NotFound);
        }, out _);

        await sync.SyncAllRepositoriesAsync(reposRoot);

        var state = JsonSerializer.Deserialize<Dictionary<string, string>>(File.ReadAllText(_stateFile))!;
        Assert.True(state.ContainsKey(filePath));
        Assert.Equal(ComputeHash(filePath), state[filePath]);
    }

    // -------------------------------------------------------------------------
    // SyncDirectoryAsync — attach failure: state must NOT be persisted
    // -------------------------------------------------------------------------

    [Fact]
    public async Task SyncDirectory_AttachFails_StateNotSaved()
    {
        var reposRoot = Path.Combine(_tempRoot, "repos");
        var repoDir = Path.Combine(reposRoot, "myrepo");
        Directory.CreateDirectory(repoDir);
        File.WriteAllText(Path.Combine(repoDir, "doc.md"), "# Doc");

        var sync = CreateSync(req =>
        {
            var url = req.RequestUri!.ToString();
            if (req.Method == HttpMethod.Get)
                return JsonOk("""[{"id":"kb-1","name":"myrepo"}]""");
            if (req.Method == HttpMethod.Post && url.EndsWith("/api/v1/files/"))
                return JsonOk("""{"id":"file-001"}""");
            // 400 Bad Request is not retried by ShouldRetry, so the test stays fast.
            if (req.Method == HttpMethod.Post && url.Contains("/file/add"))
                return new HttpResponseMessage(HttpStatusCode.BadRequest);
            return new HttpResponseMessage(HttpStatusCode.NotFound);
        }, out _);

        await sync.SyncAllRepositoriesAsync(reposRoot);

        Assert.False(File.Exists(_stateFile), "State file must not be written when attach fails.");
    }

    [Fact]
    public async Task SyncDirectory_SingleFileAttach_SavesState()
    {
        var reposRoot = Path.Combine(_tempRoot, "repos");
        var repoDir = Path.Combine(reposRoot, "myrepo");
        Directory.CreateDirectory(repoDir);
        var filePath = Path.Combine(repoDir, "doc.md");
        File.WriteAllText(filePath, "# Doc");

        var sync = CreateSync(req =>
        {
            var url = req.RequestUri!.ToString();
            if (req.Method == HttpMethod.Get && url.Contains("/api/v1/knowledge/"))
                return JsonOk("""[{"id":"kb-1","name":"myrepo"}]""");
            if (req.Method == HttpMethod.Post && url.EndsWith("/api/v1/files/"))
                return JsonOk("""{"id":"file-001"}""");
            if (req.Method == HttpMethod.Post && url.Contains("/file/add"))
                return JsonOk("{}");
            return new HttpResponseMessage(HttpStatusCode.NotFound);
        }, out var fake);

        await sync.SyncAllRepositoriesAsync(reposRoot);

        Assert.True(File.Exists(_stateFile), "State file should be written when single-file attach succeeds.");
        Assert.Contains(fake.RequestLog, r => r.Method == "POST" && r.Url.Contains("/file/add"));
    }

    // -------------------------------------------------------------------------
    // GetOrCreateKnowledgeBaseAsync — KB does not exist, creation path
    // -------------------------------------------------------------------------

    [Fact]
    public async Task SyncDirectory_KbNotFound_CreatesNewKbThenUploads()
    {
        var reposRoot = Path.Combine(_tempRoot, "repos");
        var repoDir = Path.Combine(reposRoot, "myrepo");
        Directory.CreateDirectory(repoDir);
        File.WriteAllText(Path.Combine(repoDir, "doc.md"), "# Doc");

        var sync = CreateSync(req =>
        {
            var url = req.RequestUri!.ToString();
            // KB list returns empty — "myrepo" not found.
            if (req.Method == HttpMethod.Get && url.Contains("/api/v1/knowledge/"))
                return JsonOk("[]");
            // KB creation.
            if (req.Method == HttpMethod.Post && url.Contains("/api/v1/knowledge/create"))
                return JsonOk("""{"id":"new-kb"}""");
            // File upload.
            if (req.Method == HttpMethod.Post && url.EndsWith("/api/v1/files/"))
                return JsonOk("""{"id":"file-001"}""");
            // Single-file attach.
            if (req.Method == HttpMethod.Post && url.Contains("/file/add"))
                return JsonOk("{}");
            return new HttpResponseMessage(HttpStatusCode.NotFound);
        }, out var fake);

        await sync.SyncAllRepositoriesAsync(reposRoot);

        // 1 GET (list) + 1 POST (create) + 1 POST (upload) + 1 POST (single-file attach) = 4 requests.
        Assert.Equal(4, fake.RequestLog.Count);
        Assert.True(File.Exists(_stateFile));
    }

    // -------------------------------------------------------------------------
    // State persistence — re-run skips already-synced files
    // -------------------------------------------------------------------------

    [Fact]
    public async Task SecondRun_WithSavedState_SkipsAlreadySyncedFiles()
    {
        var reposRoot = Path.Combine(_tempRoot, "repos");
        var repoDir = Path.Combine(reposRoot, "myrepo");
        Directory.CreateDirectory(repoDir);
        File.WriteAllText(Path.Combine(repoDir, "doc.md"), "# Doc");

        Func<HttpRequestMessage, HttpResponseMessage> handler = req =>
        {
            var url = req.RequestUri!.ToString();
            if (req.Method == HttpMethod.Get)
                return JsonOk("""[{"id":"kb-1","name":"myrepo"}]""");
            if (req.Method == HttpMethod.Post && url.EndsWith("/api/v1/files/"))
                return JsonOk("""{"id":"file-001"}""");
            if (req.Method == HttpMethod.Post && url.Contains("/file/add"))
                return JsonOk("{}");
            return new HttpResponseMessage(HttpStatusCode.NotFound);
        };

        // First run — uploads file and saves state.
        var sync1 = CreateSync(handler, out _);
        await sync1.SyncAllRepositoriesAsync(reposRoot);
        Assert.True(File.Exists(_stateFile));

        // Second run — loads saved state, file is unchanged, no upload or attach.
        var sync2 = CreateSync(handler, out var fake2);
        await sync2.SyncAllRepositoriesAsync(reposRoot);

        Assert.Single(fake2.RequestLog); // Only the KB GET.
        Assert.Equal("GET", fake2.RequestLog.First().Method);
    }
}
