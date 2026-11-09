-- Run once in SSMS using an account allowed to create tables in Employee_db.
-- This script does not import photos or change existing employee records.
USE [Employee_db];
GO
SET NOCOUNT ON;
SET XACT_ABORT ON;

IF OBJECT_ID(N'dbo.EventDeskEmployeePhoto', N'U') IS NULL
BEGIN
    CREATE TABLE dbo.EventDeskEmployeePhoto
    (
        EmployeeId varchar(50) COLLATE Latin1_General_100_BIN2 NOT NULL,
        Photo varbinary(max) NOT NULL,
        PhotoHash binary(32) NOT NULL,
        ContentType varchar(32) NOT NULL
            CONSTRAINT DF_EventDeskEmployeePhoto_ContentType DEFAULT ('image/jpeg'),
        UpdatedAt datetime2(3) NOT NULL
            CONSTRAINT DF_EventDeskEmployeePhoto_UpdatedAt DEFAULT (SYSUTCDATETIME()),
        ImportBatchId uniqueidentifier NOT NULL,
        CONSTRAINT PK_EventDeskEmployeePhoto PRIMARY KEY (EmployeeId),
        CONSTRAINT CK_EventDeskEmployeePhoto_EmployeeId CHECK
            (LEN(EmployeeId) BETWEEN 1 AND 50 AND EmployeeId NOT LIKE '%[^0-9A-Za-z_-]%'),
        CONSTRAINT CK_EventDeskEmployeePhoto_Bytes CHECK
            (DATALENGTH(Photo) BETWEEN 3 AND 262144),
        CONSTRAINT CK_EventDeskEmployeePhoto_Jpeg CHECK
            (SUBSTRING(Photo, 1, 3) = 0xFFD8FF AND ContentType = 'image/jpeg')
    );
    PRINT 'Employee photo table created.';
END
ELSE
BEGIN
    PRINT 'Employee photo table already exists; no schema changes were made.';
END;

SELECT c.name AS ColumnName, t.name AS DataType, c.max_length AS MaxLength,
       c.is_nullable AS IsNullable
FROM sys.columns AS c
JOIN sys.types AS t ON t.user_type_id = c.user_type_id
WHERE c.object_id = OBJECT_ID(N'dbo.EventDeskEmployeePhoto', N'U')
ORDER BY c.column_id;
