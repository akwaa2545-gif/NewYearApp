using System;
using System.Collections.Generic;
using System.Drawing;
using System.Drawing.Imaging;
using System.IO;
using System.Security.Cryptography;
using System.Text;
using EventDesk.PhotoImport;

public static class ImportPhotosTests
{
    private static int checks;
    private static void Check(bool condition, string name)
    {
        checks++;
        if (!condition) throw new Exception("FAILED: " + name);
    }
    private static void Reject(Action action, string name)
    {
        bool rejected = false;
        try { action(); } catch (ImportValidationException) { rejected = true; }
        Check(rejected, name);
    }
    private sealed class Fixture : IDisposable
    {
        public readonly string Root = Path.Combine(Path.GetTempPath(), "photo-import-tests-" + Guid.NewGuid().ToString("N"));
        public readonly byte[] Data;
        public readonly string Hash;
        public string ManifestPath { get { return Path.Combine(Root, "manifest.json"); } }
        public Fixture()
        {
            Directory.CreateDirectory(Path.Combine(Root, "photos"));
            using (Bitmap bitmap = new Bitmap(32, 24))
            using (MemoryStream stream = new MemoryStream())
            {
                bitmap.Save(stream, ImageFormat.Jpeg);
                Data = stream.ToArray();
            }
            using (SHA256 sha = SHA256.Create()) Hash = BitConverter.ToString(sha.ComputeHash(Data)).Replace("-", "").ToLowerInvariant();
            File.WriteAllBytes(Path.Combine(Root, "photos", "0000001.jpg"), Data);
            Write(Entry("0000001", "photos/0000001.jpg", Hash, Data.Length, 32, 24));
        }
        public static string Entry(string id, string file, string hash, int size, int width, int height)
        {
            return "{\"employee_id\":\"" + id + "\",\"file\":\"" + file + "\",\"sha256\":\"" + hash + "\",\"bytes\":" + size + ",\"width\":" + width + ",\"height\":" + height + ",\"content_type\":\"image/jpeg\"}";
        }
        public void Write(string entries)
        {
            File.WriteAllText(ManifestPath, "{\"schema_version\":1,\"batch_id\":\"d9947560-f61b-4800-88d2-ad85d9378896\",\"employee_source_count\":5000,\"entries\":[" + entries + "]}", new UTF8Encoding(false));
        }
        public void ReplacePhoto(byte[] content)
        {
            string digest;
            using (SHA256 sha = SHA256.Create()) digest = BitConverter.ToString(sha.ComputeHash(content)).Replace("-", "").ToLowerInvariant();
            File.WriteAllBytes(Path.Combine(Root, "photos", "0000001.jpg"), content);
            Write(Entry("0000001", "photos/0000001.jpg", digest, content.Length, 32, 24));
        }
        public void Dispose() { Directory.Delete(Root, true); }
    }
    private sealed class FakeStore : IImportStore
    {
        public readonly Dictionary<string, byte[]> Existing = new Dictionary<string, byte[]>();
        public int Reads, Writes, Starts, Commits, Rollbacks;
        public bool FailWrite;
        public byte[] FindHash(string id) { Reads++; return Existing.ContainsKey(id) ? Existing[id] : null; }
        public IImportTransaction BeginTransaction() { Starts++; return new Transaction(this); }
        public void Dispose() { }
        private sealed class Transaction : IImportTransaction
        {
            private readonly FakeStore store;
            private bool committed;
            public Transaction(FakeStore value) { store = value; }
            public byte[] FindHash(string id) { return store.FindHash(id); }
            public void Insert(PhotoRecord photo, Guid batch) { Write(); }
            public void Update(PhotoRecord photo, Guid batch) { Write(); }
            private void Write() { store.Writes++; if (store.FailWrite) throw new InvalidOperationException("secret-should-not-appear"); }
            public void Commit() { committed = true; store.Commits++; }
            public void Dispose() { if (!committed) store.Rollbacks++; }
        }
    }
    private static void ValidationTests()
    {
        using (Fixture f = new Fixture())
        {
            ValidatedBatch batch = ManifestValidator.Load(f.ManifestPath);
            Check(batch.Photos.Count == 1 && batch.Photos[0].EmployeeId == "0000001", "valid JPEG manifest");
            Check(batch.Photos[0].Width == 32 && batch.Photos[0].Height == 24, "aspect ratio preserved");
            string valid = File.ReadAllText(f.ManifestPath);
            File.WriteAllText(f.ManifestPath, valid.Replace("\"schema_version\":1", "\"schema_version\":2"));
            Reject(delegate { ManifestValidator.Load(f.ManifestPath); }, "schema version");
            File.WriteAllText(f.ManifestPath, valid.Replace("\"schema_version\":1", "\"schema_version\":1,\"unexpected\":1"));
            Reject(delegate { ManifestValidator.Load(f.ManifestPath); }, "unknown root field");
            File.WriteAllText(f.ManifestPath, valid.Replace("\"schema_version\":1", "\"__type\":\"hidden\",\"schema_version\":1"));
            Reject(delegate { ManifestValidator.Load(f.ManifestPath); }, "reserved JSON field is unknown");
            File.WriteAllText(f.ManifestPath, valid.Replace("\"schema_version\":1", "\"schema_version\":1,\"schema_version\":1"));
            Reject(delegate { ManifestValidator.Load(f.ManifestPath); }, "duplicate JSON field");
            File.WriteAllText(f.ManifestPath, valid.Replace("\"width\":32", "\"width\":\"32\""));
            Reject(delegate { ManifestValidator.Load(f.ManifestPath); }, "numeric field must be number");
            File.WriteAllText(f.ManifestPath, valid.Replace("d9947560-f61b-4800-88d2-ad85d9378896", "invalid"));
            Reject(delegate { ManifestValidator.Load(f.ManifestPath); }, "invalid batch UUID");
            string[] badIds = { "1", "000000A", "0000001 ", "00000/1", new string('1', 33) };
            foreach (string id in badIds)
            {
                f.Write(Fixture.Entry(id, "photos/0000001.jpg", f.Hash, f.Data.Length, 32, 24));
                Reject(delegate { ManifestValidator.Load(f.ManifestPath); }, "noncanonical employee ID");
            }
            string[] badPaths = { "../photos/0000001.jpg", "C:/photos/0000001.jpg", "/photos/0000001.jpg", "photos/../photos/0000001.jpg", "photos/other.jpg", "photos/0000001.jpg:stream", "photos\\\\0000001.jpg" };
            foreach (string path in badPaths)
            {
                f.Write(Fixture.Entry("0000001", path, f.Hash, f.Data.Length, 32, 24));
                Reject(delegate { ManifestValidator.Load(f.ManifestPath); }, "unsafe photo path");
            }
            f.Write(Fixture.Entry("0000001", "photos/0000001.jpg", new string('0', 64), f.Data.Length, 32, 24));
            Reject(delegate { ManifestValidator.Load(f.ManifestPath); }, "hash mismatch");
            f.Write(Fixture.Entry("0000001", "photos/0000001.jpg", f.Hash, f.Data.Length + 1, 32, 24));
            Reject(delegate { ManifestValidator.Load(f.ManifestPath); }, "size mismatch");
            f.Write(Fixture.Entry("0000001", "photos/0000001.jpg", f.Hash, f.Data.Length, 31, 24));
            Reject(delegate { ManifestValidator.Load(f.ManifestPath); }, "dimension mismatch");
            f.Write(Fixture.Entry("0000001", "photos/0000001.jpg", f.Hash, f.Data.Length, 257, 24));
            Reject(delegate { ManifestValidator.Load(f.ManifestPath); }, "dimension maximum");
            string entry = Fixture.Entry("0000001", "photos/0000001.jpg", f.Hash, f.Data.Length, 32, 24);
            f.Write(entry + "," + entry);
            Reject(delegate { ManifestValidator.Load(f.ManifestPath); }, "duplicate employee IDs");
            File.WriteAllText(f.ManifestPath, valid.Replace("image/jpeg", "image/png"));
            Reject(delegate { ManifestValidator.Load(f.ManifestPath); }, "wrong declared MIME type");
            File.WriteAllText(f.ManifestPath, valid.Replace("\"height\":24", "\"height\":24,\"extra\":1"));
            Reject(delegate { ManifestValidator.Load(f.ManifestPath); }, "unknown entry field");
            File.WriteAllText(f.ManifestPath, valid.Replace("\"employee_source_count\":5000", "\"employee_source_count\":0"));
            Reject(delegate { ManifestValidator.Load(f.ManifestPath); }, "source count cannot be lower than batch");
            f.Write(Fixture.Entry("0000001", "photos/0000001.jpg", f.Hash, ManifestValidator.MaxPhotoBytes + 1, 32, 24));
            Reject(delegate { ManifestValidator.Load(f.ManifestPath); }, "photo byte limit");
            f.ReplacePhoto(new byte[f.Data.Length]);
            Reject(delegate { ManifestValidator.Load(f.ManifestPath); }, "magic checked with matching hash");
            byte[] metadata = { 255, 225, 0, 4, 12, 34 };
            byte[] withMetadata = new byte[f.Data.Length + metadata.Length];
            Array.Copy(f.Data, withMetadata, 2);
            Array.Copy(metadata, 0, withMetadata, 2, metadata.Length);
            Array.Copy(f.Data, 2, withMetadata, 2 + metadata.Length, f.Data.Length - 2);
            f.ReplacePhoto(withMetadata);
            Reject(delegate { ManifestValidator.Load(f.ManifestPath); }, "metadata before scan rejected");
            Array.Copy(f.Data, withMetadata, f.Data.Length - 2);
            Array.Copy(metadata, 0, withMetadata, f.Data.Length - 2, metadata.Length);
            withMetadata[withMetadata.Length - 2] = 255; withMetadata[withMetadata.Length - 1] = 217;
            f.ReplacePhoto(withMetadata);
            Reject(delegate { ManifestValidator.Load(f.ManifestPath); }, "metadata after scan rejected");
            byte[] trailing = new byte[f.Data.Length + 2];
            Array.Copy(f.Data, trailing, f.Data.Length); trailing[trailing.Length - 2] = 255; trailing[trailing.Length - 1] = 217;
            f.ReplacePhoto(trailing);
            Reject(delegate { ManifestValidator.Load(f.ManifestPath); }, "data after JPEG end rejected");
            File.WriteAllText(f.ManifestPath, valid);
            File.WriteAllBytes(Path.Combine(f.Root, "photos", "0000001.jpg"), new byte[f.Data.Length]);
            Reject(delegate { ManifestValidator.Load(f.ManifestPath); }, "invalid image bytes");
            File.WriteAllBytes(f.ManifestPath, new byte[ManifestValidator.MaxManifestBytes + 1]);
            Reject(delegate { ManifestValidator.Load(f.ManifestPath); }, "manifest byte limit");
            StringBuilder tooMany = new StringBuilder();
            for (int i = 0; i < 5001; i++) { if (i > 0) tooMany.Append(','); tooMany.Append(entry); }
            f.Write(tooMany.ToString());
            Reject(delegate { ManifestValidator.Load(f.ManifestPath); }, "entry count limit");
        }
    }
    private static void ImportTests()
    {
        using (Fixture f = new Fixture())
        {
            ValidatedBatch batch = ManifestValidator.Load(f.ManifestPath);
            ImportSummary local = ImportRunner.Run(batch, null, false, false);
            Check(!local.Compared && local.Total == 1, "offline dry run");
            FakeStore store = new FakeStore();
            ImportSummary dry = ImportRunner.Run(batch, store, false, false);
            Check(dry.New == 1 && store.Reads == 1 && store.Writes == 0 && store.Starts == 0, "SQL comparison is read only");
            ImportRunner.Run(batch, store, true, false);
            Check(store.Writes == 1 && store.Commits == 1, "new photo committed");
            store = new FakeStore();
            store.Existing.Add("0000001", batch.Photos[0].CopyHash());
            ImportSummary identical = ImportRunner.Run(batch, store, true, false);
            Check(identical.Identical == 1 && store.Writes == 0 && store.Commits == 1, "identical skipped");
            store = new FakeStore();
            store.Existing.Add("0000001", new byte[32]);
            Reject(delegate { ImportRunner.Run(batch, store, true, false); }, "conflict requires replace flag");
            Check(store.Writes == 0 && store.Rollbacks == 1 && store.Commits == 0, "conflict rolls back before writes");
            ImportRunner.Run(batch, store, true, true);
            Check(store.Writes == 1 && store.Commits == 1, "explicit replacement");
            store = new FakeStore(); store.FailWrite = true;
            bool failed = false;
            try { ImportRunner.Run(batch, store, true, false); } catch (InvalidOperationException) { failed = true; }
            Check(failed && store.Rollbacks == 1 && store.Commits == 0, "backend failure rolls back");
            Reject(delegate { ImportRunner.Run(batch, null, true, false); }, "commit needs store");
            // Files cannot change after validation because the validated bytes are held in memory.
            File.WriteAllBytes(Path.Combine(f.Root, "photos", "0000001.jpg"), new byte[0]);
            Check(batch.Photos[0].CopyBytes().Length == f.Data.Length, "validated photo cached");
            byte[] exposed = batch.Photos[0].CopyHash(); exposed[0] ^= 255;
            Check(exposed[0] != batch.Photos[0].CopyHash()[0], "hash immutable");
        }
        using (Fixture f = new Fixture())
        {
            File.WriteAllBytes(Path.Combine(f.Root, "photos", "0000002.jpg"), f.Data);
            f.Write(Fixture.Entry("0000001", "photos/0000001.jpg", f.Hash, f.Data.Length, 32, 24) + "," + Fixture.Entry("0000002", "photos/0000002.jpg", f.Hash, f.Data.Length, 32, 24));
            ValidatedBatch batch = ManifestValidator.Load(f.ManifestPath);
            FakeStore store = new FakeStore();
            store.Existing.Add("0000002", new byte[32]);
            Reject(delegate { ImportRunner.Run(batch, store, true, false); }, "late conflict preflight");
            Check(store.Reads == 2 && store.Writes == 0 && store.Rollbacks == 1, "whole batch checked before any write");
        }
    }
    private static void ConnectionTests()
    {
        Reject(delegate { SqlImportStore.ValidateConnection("Data Source=localhost;Initial Catalog=master;Encrypt=True"); }, "wrong database");
        Check(SqlImportStore.ValidateConnection("Data Source=localhost;Initial Catalog=Employee_db;Encrypt=True;TrustServerCertificate=True").Length > 0, "approved Employee_db accepted");
        Reject(delegate { SqlImportStore.ValidateConnection("Data Source=localhost;Initial Catalog=DBEmployee_db;Encrypt=True"); }, "incorrect database alias rejected");
        Reject(delegate { SqlImportStore.ValidateConnection("Data Source=localhost;Initial Catalog=Employee_db;Encrypt=False"); }, "encryption required");
        Reject(delegate { SqlImportStore.ValidateConnection("Data Source=localhost;Initial Catalog=Employee_db"); }, "encryption explicit");
        Reject(delegate { SqlImportStore.ValidateConnection("not a connection string"); }, "invalid connection sanitized");
        Check(SqlImportStore.ValidateConnection("Data Source=localhost;Initial Catalog=Employee_db;Encrypt=True;TrustServerCertificate=True").Length > 0, "explicit trust accepted");
    }
    private static void CommandTests()
    {
        string previous = Environment.GetEnvironmentVariable("EVENTDESK_IMPORT_CONNECTION");
        TextWriter output = Console.Out, errors = Console.Error;
        try
        {
            using (Fixture f = new Fixture())
            using (StringWriter capture = new StringWriter())
            {
                Console.SetOut(capture); Console.SetError(capture);
                Environment.SetEnvironmentVariable("EVENTDESK_IMPORT_CONNECTION", null);
                Check(EventDesk.PhotoImport.Program.Main(new string[] { "--manifest", f.ManifestPath }) == 0, "CLI offline dry run");
                Check(EventDesk.PhotoImport.Program.Main(new string[] { "--manifest", f.ManifestPath, "--commit" }) == 1, "CLI commit needs environment");
                Environment.SetEnvironmentVariable("EVENTDESK_IMPORT_CONNECTION", "secret-password-should-never-be-printed");
                Check(EventDesk.PhotoImport.Program.Main(new string[] { "--manifest", f.ManifestPath }) == 1, "CLI rejects invalid connection");
                Check(!capture.ToString().Contains("secret-password"), "credentials absent from error output");
                f.ReplacePhoto(new byte[f.Data.Length]);
                capture.GetStringBuilder().Length = 0;
                Check(EventDesk.PhotoImport.Program.Main(new string[] { "--manifest", f.ManifestPath, "--commit" }) == 1, "CLI validates before configured SQL");
                Check(capture.ToString().Contains("invalid_jpeg_magic") && !capture.ToString().Contains("connection"), "manifest failure precedes connection parsing");
            }
        }
        finally { Environment.SetEnvironmentVariable("EVENTDESK_IMPORT_CONNECTION", previous); Console.SetOut(output); Console.SetError(errors); }
    }
    public static int Main()
    {
        try { ValidationTests(); ImportTests(); ConnectionTests(); CommandTests(); Console.WriteLine("PASS: " + checks + " importer checks"); return 0; }
        catch (Exception error) { Console.Error.WriteLine(error.ToString()); return 1; }
    }
}
