// Standalone .NET Framework importer. No SQL or filesystem writes occur in local dry-run mode.
using System;
using System.Collections.Generic;
using System.Collections.ObjectModel;
using System.Data;
using System.Data.SqlClient;
using System.Drawing;
using System.Drawing.Imaging;
using System.Globalization;
using System.IO;
using System.Runtime.Serialization.Json;
using System.Security.Cryptography;
using System.Text;
using System.Text.RegularExpressions;
using System.Xml;

namespace EventDesk.PhotoImport
{
    public sealed class ImportValidationException : Exception
    {
        public ImportValidationException(string reason) : base(reason) { }
    }

    public sealed class PhotoRecord
    {
        private readonly byte[] data;
        private readonly byte[] hash;
        public string EmployeeId { get; private set; }
        public int Width { get; private set; }
        public int Height { get; private set; }
        internal PhotoRecord(string id, byte[] content, byte[] digest, int width, int height)
        {
            EmployeeId = id; data = content; hash = digest; Width = width; Height = height;
        }
        public byte[] CopyBytes() { return (byte[])data.Clone(); }
        public byte[] CopyHash() { return (byte[])hash.Clone(); }
        internal byte[] Data { get { return data; } }
        internal byte[] Hash { get { return hash; } }
    }

    public sealed class ValidatedBatch
    {
        public Guid BatchId { get; private set; }
        public int EmployeeSourceCount { get; private set; }
        public ReadOnlyCollection<PhotoRecord> Photos { get; private set; }
        internal ValidatedBatch(Guid id, int count, List<PhotoRecord> photos)
        {
            BatchId = id; EmployeeSourceCount = count;
            Photos = new ReadOnlyCollection<PhotoRecord>(photos.ToArray());
        }
    }

    public static class ManifestValidator
    {
        public const int MaxManifestBytes = 10 * 1024 * 1024;
        public const int MaxPhotoBytes = 256 * 1024;
        public const int MaxEntries = 5000;
        public const long MaxBatchBytes = 128L * 1024 * 1024;
        private static readonly Regex IdPattern = new Regex(@"\A[0-9]{7,32}\z", RegexOptions.CultureInvariant);
        private static readonly Regex HashPattern = new Regex(@"\A[0-9a-f]{64}\z", RegexOptions.CultureInvariant);

        public static ValidatedBatch Load(string manifestPath)
        {
            try { return LoadChecked(manifestPath); }
            catch (ImportValidationException) { throw; }
            catch (Exception error)
            {
                if (error is OutOfMemoryException || error is StackOverflowException) throw;
                throw new ImportValidationException("manifest_or_photo_invalid_or_unreadable");
            }
        }

        private static ValidatedBatch LoadChecked(string manifestPath)
        {
            if (String.IsNullOrWhiteSpace(manifestPath)) Fail("manifest_path_required");
            if (HasParentSegment(manifestPath)) Fail("manifest_parent_traversal");
            string fullManifest = Path.GetFullPath(manifestPath);
            RejectReparsePoints(fullManifest);
            byte[] json = ReadBounded(fullManifest, MaxManifestBytes);
            XmlDocument document = new XmlDocument();
            document.XmlResolver = null;
            XmlDictionaryReaderQuotas quotas = new XmlDictionaryReaderQuotas();
            quotas.MaxDepth = 12; quotas.MaxArrayLength = MaxEntries + 1;
            quotas.MaxStringContentLength = 1024; quotas.MaxBytesPerRead = 4096;
            using (XmlDictionaryReader reader = JsonReaderWriterFactory.CreateJsonReader(json, quotas)) document.Load(reader);
            XmlElement root = document.DocumentElement;
            Dictionary<string, XmlElement> fields = Members(root, "schema_version", "batch_id", "employee_source_count", "entries");
            if (Number(fields["schema_version"]) != 1) Fail("unsupported_schema_version");
            Guid batch;
            if (!Guid.TryParseExact(StringValue(fields["batch_id"]), "D", out batch) || batch == Guid.Empty) Fail("invalid_batch_id");
            int sourceCount = Number(fields["employee_source_count"]);
            XmlElement entries = fields["entries"];
            if (entries.GetAttribute("type") != "array") Fail("entries_must_be_array");
            if (entries.ChildNodes.Count > MaxEntries) Fail("entry_limit_exceeded");
            if (sourceCount < entries.ChildNodes.Count || sourceCount < 0) Fail("invalid_employee_source_count");
            string directory = Path.GetDirectoryName(fullManifest);
            List<PhotoRecord> photos = new List<PhotoRecord>();
            HashSet<string> ids = new HashSet<string>(StringComparer.Ordinal);
            long totalBytes = 0;
            foreach (XmlNode node in entries.ChildNodes)
            {
                XmlElement item = node as XmlElement;
                if (item == null || item.Name != "item") Fail("invalid_entry");
                PhotoRecord photo = LoadPhoto(directory, item, ids, ref totalBytes);
                photos.Add(photo);
            }
            return new ValidatedBatch(batch, sourceCount, photos);
        }

        private static PhotoRecord LoadPhoto(string directory, XmlElement item, HashSet<string> ids, ref long totalBytes)
        {
            Dictionary<string, XmlElement> fields = Members(item, "employee_id", "file", "sha256", "bytes", "width", "height", "content_type");
            string id = StringValue(fields["employee_id"]);
            if (!IdPattern.IsMatch(id)) Fail("noncanonical_employee_id");
            if (!ids.Add(id)) Fail("duplicate_employee_id");
            string relative = StringValue(fields["file"]);
            if (relative != "photos/" + id + ".jpg") Fail("noncanonical_photo_path");
            if (StringValue(fields["content_type"]) != "image/jpeg") Fail("unsupported_content_type");
            string digest = StringValue(fields["sha256"]);
            if (!HashPattern.IsMatch(digest)) Fail("invalid_sha256");
            int size = Number(fields["bytes"]), width = Number(fields["width"]), height = Number(fields["height"]);
            if (size <= 0 || size > MaxPhotoBytes) Fail("photo_byte_limit_exceeded");
            if (width < 1 || height < 1 || width > 256 || height > 256) Fail("photo_dimension_limit_exceeded");
            totalBytes += size;
            if (totalBytes > MaxBatchBytes) Fail("batch_byte_limit_exceeded");
            string path = Path.Combine(directory, "photos", id + ".jpg");
            RejectReparsePoints(path);
            byte[] content = ReadBounded(path, MaxPhotoBytes);
            if (content.Length != size) Fail("photo_size_mismatch");
            byte[] hash;
            using (SHA256 sha = SHA256.Create()) hash = sha.ComputeHash(content);
            if (Hex(hash) != digest) Fail("photo_hash_mismatch");
            ValidateJpeg(content, width, height);
            return new PhotoRecord(id, content, hash, width, height);
        }

        private static Dictionary<string, XmlElement> Members(XmlElement element, params string[] names)
        {
            if (element == null || element.GetAttribute("type") != "object" || element.Attributes.Count != 1) Fail("expected_json_object");
            HashSet<string> expected = new HashSet<string>(names, StringComparer.Ordinal);
            Dictionary<string, XmlElement> result = new Dictionary<string, XmlElement>(StringComparer.Ordinal);
            foreach (XmlNode node in element.ChildNodes)
            {
                XmlElement child = node as XmlElement;
                if (child == null || !expected.Contains(child.Name) || result.ContainsKey(child.Name)) Fail("unknown_or_duplicate_json_field");
                result.Add(child.Name, child);
            }
            if (result.Count != names.Length) Fail("missing_json_field");
            return result;
        }

        private static string StringValue(XmlElement element)
        {
            if (element.GetAttribute("type") != "string" || element.ChildNodes.Count > 1 || element.FirstChild is XmlElement) Fail("expected_json_string");
            return element.InnerText;
        }

        private static int Number(XmlElement element)
        {
            int value;
            if (element.GetAttribute("type") != "number" || !Int32.TryParse(element.InnerText, NumberStyles.None, CultureInfo.InvariantCulture, out value))
                throw new ImportValidationException("expected_nonnegative_integer");
            return value;
        }

        private static bool HasParentSegment(string path)
        {
            foreach (string segment in path.Replace('\\', '/').Split('/')) if (segment == "..") return true;
            return false;
        }

        private static void RejectReparsePoints(string path)
        {
            string current = path;
            while (!String.IsNullOrEmpty(current))
            {
                if ((File.GetAttributes(current) & FileAttributes.ReparsePoint) != 0) Fail("reparse_points_not_allowed");
                string parent = Path.GetDirectoryName(current);
                if (parent == current) break;
                current = parent;
            }
        }

        private static byte[] ReadBounded(string path, int limit)
        {
            // FileShare.Read denies replacement/writes while this snapshot is read; SQL uses only the cached bytes.
            using (FileStream file = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.Read))
            {
                if (file.Length < 1 || file.Length > limit) Fail("file_byte_limit_exceeded");
                byte[] bytes = new byte[(int)file.Length];
                int offset = 0;
                while (offset < bytes.Length)
                {
                    int read = file.Read(bytes, offset, bytes.Length - offset);
                    if (read == 0) Fail("file_changed_during_read");
                    offset += read;
                }
                if (file.ReadByte() != -1) Fail("file_changed_during_read");
                return bytes;
            }
        }

        private static void ValidateJpeg(byte[] bytes, int width, int height)
        {
            if (bytes.Length < 4 || bytes[0] != 255 || bytes[1] != 216 || bytes[bytes.Length - 2] != 255 || bytes[bytes.Length - 1] != 217) Fail("invalid_jpeg_magic");
            ValidateJpegHeaders(bytes, width, height);
            using (MemoryStream stream = new MemoryStream(bytes, false))
            using (Image image = Image.FromStream(stream, false, true))
            {
                if (image.RawFormat.Guid != ImageFormat.Jpeg.Guid || image.Width != width || image.Height != height) Fail("photo_format_or_dimensions_mismatch");
                // Force a full decode before any SQL connection is opened.
                using (Bitmap decoded = new Bitmap(image)) { decoded.GetPixel(width - 1, height - 1); }
            }
        }

        private static void ValidateJpegHeaders(byte[] bytes, int width, int height)
        {
            int offset = 2;
            bool dimensionsSeen = false, scanSeen = false, entropy = false;
            while (offset < bytes.Length - 2)
            {
                if (entropy)
                {
                    // Scan entropy-coded data without treating escaped FF bytes or restart markers as metadata.
                    while (offset < bytes.Length - 1)
                    {
                        if (bytes[offset++] != 255) continue;
                        while (offset < bytes.Length && bytes[offset] == 255) offset++;
                        if (offset >= bytes.Length) Fail("invalid_jpeg_scan");
                        int next = bytes[offset];
                        if (next == 0 || (next >= 208 && next <= 215)) { offset++; continue; }
                        offset--;
                        break;
                    }
                    entropy = false;
                }
                if (bytes[offset++] != 255) Fail("invalid_jpeg_segment");
                while (offset < bytes.Length && bytes[offset] == 255) offset++;
                if (offset >= bytes.Length) Fail("invalid_jpeg_segment");
                int marker = bytes[offset++];
                if (marker == 217)
                {
                    if (!dimensionsSeen || !scanSeen || offset != bytes.Length) Fail("invalid_jpeg_end");
                    return;
                }
                if (marker == 216 || marker == 0 || marker == 1 || (marker >= 208 && marker <= 215)) Fail("invalid_jpeg_segment");
                if (offset + 2 > bytes.Length) Fail("invalid_jpeg_segment");
                int length = (bytes[offset] << 8) | bytes[offset + 1];
                if (length < 2 || offset + length > bytes.Length) Fail("invalid_jpeg_segment");
                if ((marker >= 225 && marker <= 239) || marker == 254) Fail("jpeg_metadata_not_allowed");
                if (marker == 224 && (length != 16 || Encoding.ASCII.GetString(bytes, offset + 2, 5) != "JFIF\0" ||
                    bytes[offset + 14] != 0 || bytes[offset + 15] != 0)) Fail("jpeg_metadata_not_allowed");
                if (marker == 220) Fail("jpeg_dimension_changes_not_allowed");
                bool startOfFrame = marker >= 192 && marker <= 207 && marker != 196 && marker != 200 && marker != 204;
                if (startOfFrame)
                {
                    if (length < 8 || dimensionsSeen) Fail("invalid_jpeg_dimensions");
                    int actualHeight = (bytes[offset + 3] << 8) | bytes[offset + 4];
                    int actualWidth = (bytes[offset + 5] << 8) | bytes[offset + 6];
                    if (actualWidth != width || actualHeight != height) Fail("photo_dimension_mismatch");
                    dimensionsSeen = true;
                }
                if (marker == 218)
                {
                    if (!dimensionsSeen) Fail("missing_jpeg_dimensions");
                    scanSeen = true; entropy = true;
                }
                offset += length;
            }
            Fail("incomplete_jpeg");
        }

        private static string Hex(byte[] hash) { return BitConverter.ToString(hash).Replace("-", "").ToLowerInvariant(); }
        private static void Fail(string reason) { throw new ImportValidationException(reason); }
    }

    public interface IImportStore : IDisposable
    {
        byte[] FindHash(string employeeId);
        IImportTransaction BeginTransaction();
    }
    public interface IImportTransaction : IDisposable
    {
        byte[] FindHash(string employeeId);
        void Insert(PhotoRecord photo, Guid batchId);
        void Update(PhotoRecord photo, Guid batchId);
        void Commit();
    }

    public sealed class ImportSummary
    {
        public int Total { get; private set; }
        public int New { get; private set; }
        public int Different { get; private set; }
        public int Identical { get; private set; }
        public bool Compared { get; private set; }
        internal ImportSummary(int total, int added, int different, int identical, bool compared)
        { Total = total; New = added; Different = different; Identical = identical; Compared = compared; }
    }

    public static class ImportRunner
    {
        public static ImportSummary Run(ValidatedBatch batch, IImportStore store, bool commit, bool replaceExisting)
        {
            if (batch == null) throw new ImportValidationException("validated_batch_required");
            if (store == null)
            {
                if (commit) throw new ImportValidationException("commit_requires_connection");
                return new ImportSummary(batch.Photos.Count, 0, 0, 0, false);
            }
            if (!commit) return Compare(batch, store.FindHash, null);
            using (IImportTransaction transaction = store.BeginTransaction())
            {
                List<int> actions = new List<int>();
                ImportSummary result = Compare(batch, transaction.FindHash, actions);
                if (result.Different > 0 && !replaceExisting) throw new ImportValidationException("existing_photo_conflict_requires_replace_existing");
                for (int i = 0; i < batch.Photos.Count; i++)
                {
                    if (actions[i] == 1) transaction.Insert(batch.Photos[i], batch.BatchId);
                    if (actions[i] == 2) transaction.Update(batch.Photos[i], batch.BatchId);
                }
                transaction.Commit();
                return result;
            }
        }

        private static ImportSummary Compare(ValidatedBatch batch, Func<string, byte[]> find, List<int> actions)
        {
            int added = 0, different = 0, identical = 0;
            foreach (PhotoRecord photo in batch.Photos)
            {
                byte[] existing = find(photo.EmployeeId);
                int action;
                if (existing == null) { added++; action = 1; }
                else if (SameHash(existing, photo.Hash)) { identical++; action = 0; }
                else { different++; action = 2; }
                if (actions != null) actions.Add(action);
            }
            return new ImportSummary(batch.Photos.Count, added, different, identical, true);
        }
        private static bool SameHash(byte[] first, byte[] second)
        {
            if (first.Length != second.Length) return false;
            int difference = 0;
            for (int i = 0; i < first.Length; i++) difference |= first[i] ^ second[i];
            return difference == 0;
        }
    }

    public sealed class SqlImportStore : IImportStore
    {
        private readonly SqlConnection connection;
        public static string ValidateConnection(string value)
        {
            try
            {
                SqlConnectionStringBuilder builder = new SqlConnectionStringBuilder(value);
                if (!String.Equals(builder.InitialCatalog, "Employee_db", StringComparison.OrdinalIgnoreCase) || !builder.Encrypt ||
                    !String.IsNullOrEmpty(builder.AttachDBFilename) || builder.UserInstance || String.IsNullOrWhiteSpace(builder.DataSource))
                    throw new ImportValidationException("connection_requires_Employee_db_and_Encrypt_True");
                builder.ApplicationName = "EventDeskPhotoImport";
                return builder.ConnectionString;
            }
            catch (ImportValidationException) { throw; }
            catch (Exception) { throw new ImportValidationException("invalid_connection_configuration"); }
        }
        public SqlImportStore(string connectionString)
        {
            connection = new SqlConnection(ValidateConnection(connectionString));
            try { connection.Open(); } catch { connection.Dispose(); throw; }
        }
        public byte[] FindHash(string id) { return FindHash(connection, null, id); }
        public IImportTransaction BeginTransaction() { return new Transaction(connection); }
        public void Dispose() { connection.Dispose(); }

        private static byte[] FindHash(SqlConnection connection, SqlTransaction transaction, string id)
        {
            string sql = "SELECT PhotoHash FROM dbo.EventDeskEmployeePhoto" + (transaction == null ? "" : " WITH (UPDLOCK, HOLDLOCK)") + " WHERE EmployeeId = @id";
            using (SqlCommand command = new SqlCommand(sql, connection, transaction))
            {
                command.CommandTimeout = 30;
                command.Parameters.Add("@id", SqlDbType.VarChar, 50).Value = id;
                object value = command.ExecuteScalar();
                if (value == DBNull.Value) throw new ImportValidationException("existing_photo_hash_is_null");
                return (byte[])value;
            }
        }

        private sealed class Transaction : IImportTransaction
        {
            private readonly SqlConnection connection;
            private readonly SqlTransaction transaction;
            public Transaction(SqlConnection value) { connection = value; transaction = connection.BeginTransaction(IsolationLevel.Serializable); }
            public byte[] FindHash(string id) { return SqlImportStore.FindHash(connection, transaction, id); }
            public void Insert(PhotoRecord photo, Guid batchId)
            {
                Write("INSERT INTO dbo.EventDeskEmployeePhoto (EmployeeId, Photo, PhotoHash, ContentType, UpdatedAt, ImportBatchId) VALUES (@id, @photo, @hash, @mime, SYSUTCDATETIME(), @batch)", photo, batchId);
            }
            public void Update(PhotoRecord photo, Guid batchId)
            {
                Write("UPDATE dbo.EventDeskEmployeePhoto SET Photo=@photo, PhotoHash=@hash, ContentType=@mime, UpdatedAt=SYSUTCDATETIME(), ImportBatchId=@batch WHERE EmployeeId=@id", photo, batchId);
            }
            private void Write(string sql, PhotoRecord photo, Guid batchId)
            {
                using (SqlCommand command = new SqlCommand(sql, connection, transaction))
                {
                    command.CommandTimeout = 30;
                    command.Parameters.Add("@id", SqlDbType.VarChar, 50).Value = photo.EmployeeId;
                    command.Parameters.Add("@photo", SqlDbType.VarBinary, -1).Value = photo.Data;
                    command.Parameters.Add("@hash", SqlDbType.Binary, 32).Value = photo.Hash;
                    command.Parameters.Add("@mime", SqlDbType.VarChar, 32).Value = "image/jpeg";
                    command.Parameters.Add("@batch", SqlDbType.UniqueIdentifier).Value = batchId;
                    if (command.ExecuteNonQuery() != 1) throw new ImportValidationException("unexpected_affected_row_count");
                }
            }
            public void Commit() { transaction.Commit(); }
            // SqlTransaction.Dispose rolls back an uncommitted transaction, including every failure path.
            public void Dispose() { transaction.Dispose(); }
        }
    }

    public static class Program
    {
        public static int Main(string[] args)
        {
            try
            {
                string manifest = null;
                bool commit = false, replace = false;
                for (int i = 0; i < args.Length; i++)
                {
                    if (args[i] == "--manifest" && manifest == null && i + 1 < args.Length) manifest = args[++i];
                    else if (args[i] == "--commit" && !commit) commit = true;
                    else if (args[i] == "--replace-existing" && !replace) replace = true;
                    else throw new ImportValidationException("usage_--manifest_PATH_[--commit]_[--replace-existing]");
                }
                if (manifest == null) throw new ImportValidationException("manifest_path_required");
                ValidatedBatch batch = ManifestValidator.Load(manifest);
                string configured = Environment.GetEnvironmentVariable("EVENTDESK_IMPORT_CONNECTION");
                if (commit && String.IsNullOrWhiteSpace(configured)) throw new ImportValidationException("commit_requires_EVENTDESK_IMPORT_CONNECTION");
                using (IImportStore store = String.IsNullOrWhiteSpace(configured) ? null : new SqlImportStore(configured))
                {
                    ImportSummary result = ImportRunner.Run(batch, store, commit, replace);
                    string status = commit ? "committed" : result.Compared ? "dry_run_compared" : "validated_local_dry_run";
                    Console.WriteLine("status=" + status + " total=" + result.Total + " new=" + result.New + " different=" + result.Different + " identical=" + result.Identical);
                }
                return 0;
            }
            catch (ImportValidationException error) { Console.Error.WriteLine("import_rejected: " + error.Message); return 1; }
            catch (SqlException error) { Console.Error.WriteLine("sql_failed: number=" + error.Number + "; batch_not_confirmed_committed"); return 2; }
            catch (Exception) { Console.Error.WriteLine("import_failed: batch_not_confirmed_committed"); return 3; }
        }
    }
}
