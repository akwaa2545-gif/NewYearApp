-- Native event tables, in the SAME Employee_db database as the existing photos.
-- Minimum: SQL Server 2014 (12.x), database compatibility level 120.
-- This script does not upgrade SQL Server or change database compatibility.
USE [Employee_db];
GO

IF SCHEMA_ID(N'eventdesk') IS NULL EXEC(N'CREATE SCHEMA [eventdesk]')
GO

IF OBJECT_ID(N'[eventdesk].[employees]', N'U') IS NULL CREATE TABLE [eventdesk].[employees] ([id] BIGINT IDENTITY(1,1) NOT NULL,
[employee_id] NVARCHAR(128) COLLATE Latin1_General_100_BIN2 NOT NULL,
[name] NVARCHAR(400) COLLATE Latin1_General_100_CI_AS NOT NULL,
[department] NVARCHAR(MAX) NOT NULL DEFAULT '',
[qr_token] NVARCHAR(128) COLLATE Latin1_General_100_BIN2 NOT NULL,
[checked_in_at] NVARCHAR(400) COLLATE Latin1_General_100_BIN2 NULL,
[verified_at] NVARCHAR(MAX) NULL,
[extra_json] NVARCHAR(MAX) NOT NULL DEFAULT '{}',
PRIMARY KEY ([id]))
GO

IF OBJECT_ID(N'[eventdesk].[winners]', N'U') IS NULL CREATE TABLE [eventdesk].[winners] ([id] BIGINT IDENTITY(1,1) NOT NULL,
[employee_id] BIGINT NOT NULL,
[prize] NVARCHAR(MAX) NOT NULL,
[drawn_at] NVARCHAR(MAX) NOT NULL,
[drawn_by] NVARCHAR(MAX) NOT NULL,
[status] NVARCHAR(MAX) NOT NULL DEFAULT 'active',
PRIMARY KEY ([id]))
GO

IF OBJECT_ID(N'[eventdesk].[event_log]', N'U') IS NULL CREATE TABLE [eventdesk].[event_log] ([id] BIGINT IDENTITY(1,1) NOT NULL,
[employee_id] BIGINT NULL,
[action] NVARCHAR(MAX) NOT NULL,
[actor] NVARCHAR(MAX) NOT NULL,
[occurred_at] NVARCHAR(MAX) NOT NULL,
PRIMARY KEY ([id]))
GO

IF OBJECT_ID(N'[eventdesk].[schema_version]', N'U') IS NULL CREATE TABLE [eventdesk].[schema_version] ([singleton] BIGINT NOT NULL,
[version] BIGINT NOT NULL,
PRIMARY KEY ([singleton]),
CHECK ([singleton] = 1))
GO

IF OBJECT_ID(N'[eventdesk].[migrations]', N'U') IS NULL CREATE TABLE [eventdesk].[migrations] ([version] BIGINT NOT NULL,
[name] NVARCHAR(MAX) NOT NULL,
[applied_at] NVARCHAR(MAX) NOT NULL,
PRIMARY KEY ([version]))
GO

IF OBJECT_ID(N'[eventdesk].[users]', N'U') IS NULL CREATE TABLE [eventdesk].[users] ([id] BIGINT IDENTITY(1,1) NOT NULL,
[username] NVARCHAR(400) COLLATE Latin1_General_100_CI_AS NOT NULL,
[display_name] NVARCHAR(MAX) NOT NULL,
[password_salt] NVARCHAR(MAX) NOT NULL,
[password_hash] NVARCHAR(MAX) NOT NULL,
[role] NVARCHAR(MAX) NOT NULL,
[active] BIGINT NOT NULL DEFAULT 1,
[created_at] NVARCHAR(MAX) NOT NULL,
[updated_at] NVARCHAR(MAX) NOT NULL,
PRIMARY KEY ([id]),
CHECK ([role] IN ('admin', 'checkin_staff', 'verification_staff', 'lucky_draw_operator')),
CHECK ([active] IN (0, 1)))
GO

IF OBJECT_ID(N'[eventdesk].[staff_sessions]', N'U') IS NULL CREATE TABLE [eventdesk].[staff_sessions] ([session_id] NVARCHAR(128) COLLATE Latin1_General_100_BIN2 NOT NULL,
[username] NVARCHAR(MAX) NOT NULL,
[display_name] NVARCHAR(MAX) NOT NULL,
[role] NVARCHAR(MAX) NOT NULL,
[last_seen] NVARCHAR(400) COLLATE Latin1_General_100_BIN2 NOT NULL,
[expires_at] BIGINT NOT NULL,
[ended_at] NVARCHAR(MAX) NULL,
PRIMARY KEY ([session_id]))
GO

IF OBJECT_ID(N'[eventdesk].[wristbands]', N'U') IS NULL CREATE TABLE [eventdesk].[wristbands] ([id] BIGINT IDENTITY(1,1) NOT NULL,
[employee_id] BIGINT NOT NULL,
[token] NVARCHAR(128) COLLATE Latin1_General_100_BIN2 NOT NULL,
[status] NVARCHAR(400) COLLATE Latin1_General_100_BIN2 NOT NULL,
[created_at] NVARCHAR(MAX) NOT NULL,
[issued_at] NVARCHAR(MAX) NULL,
[issued_by] NVARCHAR(MAX) NULL,
[invalidated_at] NVARCHAR(MAX) NULL,
[replaced_by_id] BIGINT NULL,
[reason] NVARCHAR(MAX) NULL,
PRIMARY KEY ([id]),
CHECK ([status] IN ('not_issued', 'issued', 'replaced', 'revoked')))
GO

IF OBJECT_ID(N'[eventdesk].[wristband_distribution_history]', N'U') IS NULL CREATE TABLE [eventdesk].[wristband_distribution_history] ([id] BIGINT IDENTITY(1,1) NOT NULL,
[wristband_id] BIGINT NOT NULL,
[employee_id] BIGINT NOT NULL,
[action] NVARCHAR(MAX) NOT NULL,
[status] NVARCHAR(MAX) NOT NULL,
[actor] NVARCHAR(MAX) NOT NULL,
[reason] NVARCHAR(MAX) NULL,
[occurred_at] NVARCHAR(MAX) NOT NULL,
PRIMARY KEY ([id]))
GO

IF OBJECT_ID(N'[eventdesk].[event_checkins]', N'U') IS NULL CREATE TABLE [eventdesk].[event_checkins] ([id] BIGINT IDENTITY(1,1) NOT NULL,
[employee_id] BIGINT NOT NULL,
[wristband_id] BIGINT NULL,
[checked_in_at] NVARCHAR(400) COLLATE Latin1_General_100_BIN2 NOT NULL,
[staff_username] NVARCHAR(MAX) NOT NULL,
[station] NVARCHAR(MAX) NULL,
[legacy_key] NVARCHAR(400) COLLATE Latin1_General_100_BIN2 NULL,
PRIMARY KEY ([id]))
GO

IF OBJECT_ID(N'[eventdesk].[checkin_wristband_exceptions]', N'U') IS NULL CREATE TABLE [eventdesk].[checkin_wristband_exceptions] ([employee_id] BIGINT NOT NULL,
[reason] NVARCHAR(MAX) NOT NULL,
[detail] NVARCHAR(MAX) NOT NULL DEFAULT '',
[recorded_by] NVARCHAR(MAX) NOT NULL,
[recorded_at] NVARCHAR(MAX) NOT NULL,
PRIMARY KEY ([employee_id]),
CHECK ([reason] IN ('lost', 'unreadable', 'other')))
GO

IF OBJECT_ID(N'[eventdesk].[attendance_removals]', N'U') IS NULL CREATE TABLE [eventdesk].[attendance_removals] ([id] BIGINT IDENTITY(1,1) NOT NULL,
[batch_id] NVARCHAR(MAX) NOT NULL,
[employee_id] BIGINT NOT NULL,
[checked_in_at] NVARCHAR(MAX) NOT NULL,
[checkin_json] NVARCHAR(MAX) NOT NULL,
[removed_by] NVARCHAR(MAX) NOT NULL,
[reason] NVARCHAR(MAX) NOT NULL,
[removed_at] NVARCHAR(MAX) NOT NULL,
PRIMARY KEY ([id]))
GO

IF OBJECT_ID(N'[eventdesk].[draw_history_removals]', N'U') IS NULL CREATE TABLE [eventdesk].[draw_history_removals] ([id] BIGINT IDENTITY(1,1) NOT NULL,
[batch_id] NVARCHAR(400) COLLATE Latin1_General_100_BIN2 NOT NULL,
[history_json] NVARCHAR(MAX) NOT NULL,
[removed_by] NVARCHAR(MAX) NOT NULL,
[reason] NVARCHAR(MAX) NOT NULL,
[removed_at] NVARCHAR(MAX) NOT NULL,
PRIMARY KEY ([id]))
GO

IF OBJECT_ID(N'[eventdesk].[identity_verification_history]', N'U') IS NULL CREATE TABLE [eventdesk].[identity_verification_history] ([id] BIGINT IDENTITY(1,1) NOT NULL,
[employee_id] BIGINT NULL,
[wristband_id] BIGINT NULL,
[result] NVARCHAR(MAX) NOT NULL,
[reason] NVARCHAR(MAX) NULL,
[verified_at] NVARCHAR(MAX) NOT NULL,
[staff_username] NVARCHAR(MAX) NOT NULL,
[scanned_token_hash] NVARCHAR(MAX) NULL,
[legacy_key] NVARCHAR(400) COLLATE Latin1_General_100_BIN2 NULL,
PRIMARY KEY ([id]),
CHECK ([result] IN ('verified', 'identity_mismatch', 'not_checked_in', 'invalid_wristband')))
GO

IF OBJECT_ID(N'[eventdesk].[import_history]', N'U') IS NULL CREATE TABLE [eventdesk].[import_history] ([id] BIGINT IDENTITY(1,1) NOT NULL,
[filename] NVARCHAR(MAX) NOT NULL,
[status] NVARCHAR(MAX) NOT NULL,
[mapping_json] NVARCHAR(MAX) NULL,
[summary_json] NVARCHAR(MAX) NULL,
[reconciliation_json] NVARCHAR(MAX) NULL,
[created_by] NVARCHAR(MAX) NOT NULL,
[created_at] NVARCHAR(MAX) NOT NULL,
[committed_at] NVARCHAR(MAX) NULL,
PRIMARY KEY ([id]))
GO

IF OBJECT_ID(N'[eventdesk].[import_previews]', N'U') IS NULL CREATE TABLE [eventdesk].[import_previews] ([id] BIGINT IDENTITY(1,1) NOT NULL,
[upload_id] NVARCHAR(400) COLLATE Latin1_General_100_BIN2 NOT NULL,
[filename] NVARCHAR(MAX) NOT NULL,
[parsed_json] NVARCHAR(MAX) NOT NULL,
[preview_json] NVARCHAR(MAX) NULL,
[mapping_json] NVARCHAR(MAX) NULL,
[status] NVARCHAR(MAX) NOT NULL,
[created_by] NVARCHAR(MAX) NOT NULL,
[created_at] NVARCHAR(MAX) NOT NULL,
[expires_at] NVARCHAR(400) COLLATE Latin1_General_100_BIN2 NOT NULL,
[committed_import_id] BIGINT NULL,
PRIMARY KEY ([id]))
GO

IF OBJECT_ID(N'[eventdesk].[lucky_draw_participants]', N'U') IS NULL CREATE TABLE [eventdesk].[lucky_draw_participants] ([id] BIGINT IDENTITY(1,1) NOT NULL,
[employee_id] BIGINT NOT NULL,
[active] BIGINT NOT NULL DEFAULT 1,
[source_import_id] BIGINT NULL,
[added_at] NVARCHAR(MAX) NOT NULL,
[removed_at] NVARCHAR(MAX) NULL,
PRIMARY KEY ([id]),
CHECK ([active] IN (0, 1)))
GO

IF OBJECT_ID(N'[eventdesk].[prizes]', N'U') IS NULL CREATE TABLE [eventdesk].[prizes] ([id] BIGINT IDENTITY(1,1) NOT NULL,
[name] NVARCHAR(400) COLLATE Latin1_General_100_CI_AS NOT NULL,
[quantity] BIGINT NOT NULL DEFAULT 1,
[active] BIGINT NOT NULL DEFAULT 1,
[created_at] NVARCHAR(MAX) NOT NULL,
[updated_at] NVARCHAR(MAX) NOT NULL,
PRIMARY KEY ([id]),
CHECK ([quantity] > 0),
CHECK ([active] IN (0, 1)))
GO

IF OBJECT_ID(N'[eventdesk].[reward_images]', N'U') IS NULL CREATE TABLE [eventdesk].[reward_images] ([prize_id] BIGINT NOT NULL,
[content_type] NVARCHAR(MAX) NOT NULL,
[data] VARBINARY(MAX) NOT NULL,
PRIMARY KEY ([prize_id]))
GO

IF OBJECT_ID(N'[eventdesk].[reward_drafts]', N'U') IS NULL CREATE TABLE [eventdesk].[reward_drafts] ([id] BIGINT IDENTITY(1,1) NOT NULL,
[source_id] BIGINT NULL,
[name] NVARCHAR(MAX) NOT NULL,
[quantity] BIGINT NOT NULL,
[action] NVARCHAR(MAX) NOT NULL DEFAULT 'save',
[content_type] NVARCHAR(MAX) NULL,
[image] VARBINARY(MAX) NULL,
[image_changed] BIGINT NOT NULL DEFAULT 0,
PRIMARY KEY ([id]))
GO

IF OBJECT_ID(N'[eventdesk].[lucky_draw_rounds]', N'U') IS NULL CREATE TABLE [eventdesk].[lucky_draw_rounds] ([id] BIGINT IDENTITY(1,1) NOT NULL,
[prize_id] BIGINT NULL,
[round_number] BIGINT NOT NULL,
[label] NVARCHAR(MAX) NULL,
[status] NVARCHAR(MAX) NOT NULL DEFAULT 'completed',
[created_at] NVARCHAR(MAX) NOT NULL,
[drawn_at] NVARCHAR(MAX) NULL,
[drawn_by] NVARCHAR(MAX) NULL,
[reroll_of_winner_id] BIGINT NULL,
[legacy_source] NVARCHAR(400) COLLATE Latin1_General_100_BIN2 NULL,
PRIMARY KEY ([id]))
GO

IF OBJECT_ID(N'[eventdesk].[lucky_draw_winners]', N'U') IS NULL CREATE TABLE [eventdesk].[lucky_draw_winners] ([id] BIGINT IDENTITY(1,1) NOT NULL,
[employee_id] BIGINT NOT NULL,
[round_id] BIGINT NOT NULL,
[prize_id] BIGINT NULL,
[prize_name] NVARCHAR(MAX) NOT NULL,
[won_at] NVARCHAR(MAX) NOT NULL,
[won_by] NVARCHAR(MAX) NOT NULL,
[status] NVARCHAR(400) COLLATE Latin1_General_100_BIN2 NOT NULL DEFAULT 'active',
[rerolled_at] NVARCHAR(MAX) NULL,
[rerolled_by] NVARCHAR(MAX) NULL,
[reroll_reason] NVARCHAR(MAX) NULL,
[legacy_winner_id] BIGINT NULL,
PRIMARY KEY ([id]),
CHECK ([status] IN ('active', 'rerolled')))
GO

IF OBJECT_ID(N'[eventdesk].[event_settings]', N'U') IS NULL CREATE TABLE [eventdesk].[event_settings] ([key] NVARCHAR(400) COLLATE Latin1_General_100_BIN2 NOT NULL,
[value_json] NVARCHAR(MAX) NOT NULL,
[updated_at] NVARCHAR(MAX) NOT NULL,
[updated_by] NVARCHAR(MAX) NOT NULL,
PRIMARY KEY ([key]))
GO

IF OBJECT_ID(N'[eventdesk].[lucky_draw_presentation]', N'U') IS NULL CREATE TABLE [eventdesk].[lucky_draw_presentation] ([singleton] BIGINT NOT NULL,
[sequence] BIGINT NOT NULL DEFAULT 0,
[phase] NVARCHAR(MAX) NOT NULL DEFAULT 'waiting',
[winner_id] BIGINT NULL,
[prize_name] NVARCHAR(MAX) NULL,
[round_name] NVARCHAR(MAX) NULL,
[winner_json] NVARCHAR(MAX) NULL,
[spin_started_at] NVARCHAR(MAX) NULL,
[reveal_at] NVARCHAR(MAX) NULL,
[updated_at] NVARCHAR(MAX) NOT NULL,
[updated_by] NVARCHAR(MAX) NOT NULL,
PRIMARY KEY ([singleton]),
CHECK ([singleton] = 1),
CHECK ([phase] IN ('waiting', 'preparing', 'spinning', 'winner_selected', 'winner_announced', 'ready_for_next')))
GO

IF OBJECT_ID(N'[eventdesk].[audit_logs]', N'U') IS NULL CREATE TABLE [eventdesk].[audit_logs] ([id] BIGINT IDENTITY(1,1) NOT NULL,
[actor] NVARCHAR(MAX) NOT NULL,
[action] NVARCHAR(MAX) NOT NULL,
[entity_type] NVARCHAR(MAX) NOT NULL,
[entity_id] NVARCHAR(MAX) NULL,
[details_json] NVARCHAR(MAX) NOT NULL DEFAULT '{}',
[occurred_at] NVARCHAR(MAX) NOT NULL,
PRIMARY KEY ([id]))
GO

IF OBJECT_ID(N'[eventdesk].[_eventdesk_projector_images]', N'U') IS NULL CREATE TABLE [eventdesk].[_eventdesk_projector_images] ([image_id] NVARCHAR(128) COLLATE Latin1_General_100_BIN2 NOT NULL,
[content_type] NVARCHAR(MAX) NOT NULL,
[image] VARBINARY(MAX) NOT NULL,
PRIMARY KEY ([image_id]))
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE object_id=OBJECT_ID(N'[eventdesk].[employees]') AND name=N'ed_employees_0') CREATE INDEX [ed_employees_0] ON [eventdesk].[employees] ([checked_in_at])
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE object_id=OBJECT_ID(N'[eventdesk].[employees]') AND name=N'ed_employees_1') CREATE INDEX [ed_employees_1] ON [eventdesk].[employees] ([name])
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE object_id=OBJECT_ID(N'[eventdesk].[employees]') AND name=N'ed_employees_2') CREATE UNIQUE INDEX [ed_employees_2] ON [eventdesk].[employees] ([qr_token])
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE object_id=OBJECT_ID(N'[eventdesk].[employees]') AND name=N'ed_employees_3') CREATE UNIQUE INDEX [ed_employees_3] ON [eventdesk].[employees] ([employee_id])
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE object_id=OBJECT_ID(N'[eventdesk].[winners]') AND name=N'ed_winners_0') CREATE UNIQUE INDEX [ed_winners_0] ON [eventdesk].[winners] ([employee_id])
GO

IF NOT EXISTS (SELECT 1 FROM sys.foreign_keys WHERE parent_object_id=OBJECT_ID(N'[eventdesk].[winners]') AND name=N'ed_fk_winners_0') ALTER TABLE [eventdesk].[winners] ADD CONSTRAINT [ed_fk_winners_0] FOREIGN KEY ([employee_id]) REFERENCES [eventdesk].[employees] ([id])
GO

IF NOT EXISTS (SELECT 1 FROM sys.foreign_keys WHERE parent_object_id=OBJECT_ID(N'[eventdesk].[event_log]') AND name=N'ed_fk_event_log_0') ALTER TABLE [eventdesk].[event_log] ADD CONSTRAINT [ed_fk_event_log_0] FOREIGN KEY ([employee_id]) REFERENCES [eventdesk].[employees] ([id])
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE object_id=OBJECT_ID(N'[eventdesk].[users]') AND name=N'ed_users_0') CREATE UNIQUE INDEX [ed_users_0] ON [eventdesk].[users] ([username])
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE object_id=OBJECT_ID(N'[eventdesk].[staff_sessions]') AND name=N'ed_staff_sessions_0') CREATE INDEX [ed_staff_sessions_0] ON [eventdesk].[staff_sessions] ([last_seen])
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE object_id=OBJECT_ID(N'[eventdesk].[wristbands]') AND name=N'ed_wristbands_0') CREATE INDEX [ed_wristbands_0] ON [eventdesk].[wristbands] ([token],[status])
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE object_id=OBJECT_ID(N'[eventdesk].[wristbands]') AND name=N'ed_wristbands_1') CREATE UNIQUE INDEX [ed_wristbands_1] ON [eventdesk].[wristbands] ([employee_id]) WHERE [status] IN ('not_issued', 'issued')
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE object_id=OBJECT_ID(N'[eventdesk].[wristbands]') AND name=N'ed_wristbands_2') CREATE UNIQUE INDEX [ed_wristbands_2] ON [eventdesk].[wristbands] ([token])
GO

IF NOT EXISTS (SELECT 1 FROM sys.foreign_keys WHERE parent_object_id=OBJECT_ID(N'[eventdesk].[wristbands]') AND name=N'ed_fk_wristbands_0') ALTER TABLE [eventdesk].[wristbands] ADD CONSTRAINT [ed_fk_wristbands_0] FOREIGN KEY ([replaced_by_id]) REFERENCES [eventdesk].[wristbands] ([id])
GO

IF NOT EXISTS (SELECT 1 FROM sys.foreign_keys WHERE parent_object_id=OBJECT_ID(N'[eventdesk].[wristbands]') AND name=N'ed_fk_wristbands_1') ALTER TABLE [eventdesk].[wristbands] ADD CONSTRAINT [ed_fk_wristbands_1] FOREIGN KEY ([employee_id]) REFERENCES [eventdesk].[employees] ([id])
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE object_id=OBJECT_ID(N'[eventdesk].[wristband_distribution_history]') AND name=N'ed_wristband_distribution_history_0') CREATE INDEX [ed_wristband_distribution_history_0] ON [eventdesk].[wristband_distribution_history] ([employee_id],[id])
GO

IF NOT EXISTS (SELECT 1 FROM sys.foreign_keys WHERE parent_object_id=OBJECT_ID(N'[eventdesk].[wristband_distribution_history]') AND name=N'ed_fk_wristband_distribution_history_0') ALTER TABLE [eventdesk].[wristband_distribution_history] ADD CONSTRAINT [ed_fk_wristband_distribution_history_0] FOREIGN KEY ([employee_id]) REFERENCES [eventdesk].[employees] ([id])
GO

IF NOT EXISTS (SELECT 1 FROM sys.foreign_keys WHERE parent_object_id=OBJECT_ID(N'[eventdesk].[wristband_distribution_history]') AND name=N'ed_fk_wristband_distribution_history_1') ALTER TABLE [eventdesk].[wristband_distribution_history] ADD CONSTRAINT [ed_fk_wristband_distribution_history_1] FOREIGN KEY ([wristband_id]) REFERENCES [eventdesk].[wristbands] ([id])
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE object_id=OBJECT_ID(N'[eventdesk].[event_checkins]') AND name=N'ed_event_checkins_0') CREATE INDEX [ed_event_checkins_0] ON [eventdesk].[event_checkins] ([checked_in_at])
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE object_id=OBJECT_ID(N'[eventdesk].[event_checkins]') AND name=N'ed_event_checkins_1') CREATE UNIQUE INDEX [ed_event_checkins_1] ON [eventdesk].[event_checkins] ([legacy_key]) WHERE [legacy_key] IS NOT NULL
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE object_id=OBJECT_ID(N'[eventdesk].[event_checkins]') AND name=N'ed_event_checkins_2') CREATE UNIQUE INDEX [ed_event_checkins_2] ON [eventdesk].[event_checkins] ([employee_id])
GO

IF NOT EXISTS (SELECT 1 FROM sys.foreign_keys WHERE parent_object_id=OBJECT_ID(N'[eventdesk].[event_checkins]') AND name=N'ed_fk_event_checkins_0') ALTER TABLE [eventdesk].[event_checkins] ADD CONSTRAINT [ed_fk_event_checkins_0] FOREIGN KEY ([wristband_id]) REFERENCES [eventdesk].[wristbands] ([id])
GO

IF NOT EXISTS (SELECT 1 FROM sys.foreign_keys WHERE parent_object_id=OBJECT_ID(N'[eventdesk].[event_checkins]') AND name=N'ed_fk_event_checkins_1') ALTER TABLE [eventdesk].[event_checkins] ADD CONSTRAINT [ed_fk_event_checkins_1] FOREIGN KEY ([employee_id]) REFERENCES [eventdesk].[employees] ([id])
GO

IF NOT EXISTS (SELECT 1 FROM sys.foreign_keys WHERE parent_object_id=OBJECT_ID(N'[eventdesk].[checkin_wristband_exceptions]') AND name=N'ed_fk_checkin_wristband_exceptions_0') ALTER TABLE [eventdesk].[checkin_wristband_exceptions] ADD CONSTRAINT [ed_fk_checkin_wristband_exceptions_0] FOREIGN KEY ([employee_id]) REFERENCES [eventdesk].[employees] ([id])
GO

IF NOT EXISTS (SELECT 1 FROM sys.foreign_keys WHERE parent_object_id=OBJECT_ID(N'[eventdesk].[attendance_removals]') AND name=N'ed_fk_attendance_removals_0') ALTER TABLE [eventdesk].[attendance_removals] ADD CONSTRAINT [ed_fk_attendance_removals_0] FOREIGN KEY ([employee_id]) REFERENCES [eventdesk].[employees] ([id])
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE object_id=OBJECT_ID(N'[eventdesk].[draw_history_removals]') AND name=N'ed_draw_history_removals_0') CREATE UNIQUE INDEX [ed_draw_history_removals_0] ON [eventdesk].[draw_history_removals] ([batch_id])
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE object_id=OBJECT_ID(N'[eventdesk].[identity_verification_history]') AND name=N'ed_identity_verification_history_0') CREATE INDEX [ed_identity_verification_history_0] ON [eventdesk].[identity_verification_history] ([employee_id],[id])
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE object_id=OBJECT_ID(N'[eventdesk].[identity_verification_history]') AND name=N'ed_identity_verification_history_1') CREATE UNIQUE INDEX [ed_identity_verification_history_1] ON [eventdesk].[identity_verification_history] ([legacy_key]) WHERE [legacy_key] IS NOT NULL
GO

IF NOT EXISTS (SELECT 1 FROM sys.foreign_keys WHERE parent_object_id=OBJECT_ID(N'[eventdesk].[identity_verification_history]') AND name=N'ed_fk_identity_verification_history_0') ALTER TABLE [eventdesk].[identity_verification_history] ADD CONSTRAINT [ed_fk_identity_verification_history_0] FOREIGN KEY ([wristband_id]) REFERENCES [eventdesk].[wristbands] ([id])
GO

IF NOT EXISTS (SELECT 1 FROM sys.foreign_keys WHERE parent_object_id=OBJECT_ID(N'[eventdesk].[identity_verification_history]') AND name=N'ed_fk_identity_verification_history_1') ALTER TABLE [eventdesk].[identity_verification_history] ADD CONSTRAINT [ed_fk_identity_verification_history_1] FOREIGN KEY ([employee_id]) REFERENCES [eventdesk].[employees] ([id])
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE object_id=OBJECT_ID(N'[eventdesk].[import_previews]') AND name=N'ed_import_previews_0') CREATE INDEX [ed_import_previews_0] ON [eventdesk].[import_previews] ([expires_at])
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE object_id=OBJECT_ID(N'[eventdesk].[import_previews]') AND name=N'ed_import_previews_1') CREATE UNIQUE INDEX [ed_import_previews_1] ON [eventdesk].[import_previews] ([upload_id])
GO

IF NOT EXISTS (SELECT 1 FROM sys.foreign_keys WHERE parent_object_id=OBJECT_ID(N'[eventdesk].[import_previews]') AND name=N'ed_fk_import_previews_0') ALTER TABLE [eventdesk].[import_previews] ADD CONSTRAINT [ed_fk_import_previews_0] FOREIGN KEY ([committed_import_id]) REFERENCES [eventdesk].[import_history] ([id])
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE object_id=OBJECT_ID(N'[eventdesk].[lucky_draw_participants]') AND name=N'ed_lucky_draw_participants_0') CREATE UNIQUE INDEX [ed_lucky_draw_participants_0] ON [eventdesk].[lucky_draw_participants] ([employee_id])
GO

IF NOT EXISTS (SELECT 1 FROM sys.foreign_keys WHERE parent_object_id=OBJECT_ID(N'[eventdesk].[lucky_draw_participants]') AND name=N'ed_fk_lucky_draw_participants_0') ALTER TABLE [eventdesk].[lucky_draw_participants] ADD CONSTRAINT [ed_fk_lucky_draw_participants_0] FOREIGN KEY ([source_import_id]) REFERENCES [eventdesk].[import_history] ([id])
GO

IF NOT EXISTS (SELECT 1 FROM sys.foreign_keys WHERE parent_object_id=OBJECT_ID(N'[eventdesk].[lucky_draw_participants]') AND name=N'ed_fk_lucky_draw_participants_1') ALTER TABLE [eventdesk].[lucky_draw_participants] ADD CONSTRAINT [ed_fk_lucky_draw_participants_1] FOREIGN KEY ([employee_id]) REFERENCES [eventdesk].[employees] ([id])
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE object_id=OBJECT_ID(N'[eventdesk].[prizes]') AND name=N'ed_prizes_0') CREATE UNIQUE INDEX [ed_prizes_0] ON [eventdesk].[prizes] ([name])
GO

IF NOT EXISTS (SELECT 1 FROM sys.foreign_keys WHERE parent_object_id=OBJECT_ID(N'[eventdesk].[reward_images]') AND name=N'ed_fk_reward_images_0') ALTER TABLE [eventdesk].[reward_images] ADD CONSTRAINT [ed_fk_reward_images_0] FOREIGN KEY ([prize_id]) REFERENCES [eventdesk].[prizes] ([id])
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE object_id=OBJECT_ID(N'[eventdesk].[reward_drafts]') AND name=N'ed_reward_drafts_0') CREATE UNIQUE INDEX [ed_reward_drafts_0] ON [eventdesk].[reward_drafts] ([source_id]) WHERE [source_id] IS NOT NULL
GO

IF NOT EXISTS (SELECT 1 FROM sys.foreign_keys WHERE parent_object_id=OBJECT_ID(N'[eventdesk].[reward_drafts]') AND name=N'ed_fk_reward_drafts_0') ALTER TABLE [eventdesk].[reward_drafts] ADD CONSTRAINT [ed_fk_reward_drafts_0] FOREIGN KEY ([source_id]) REFERENCES [eventdesk].[prizes] ([id])
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE object_id=OBJECT_ID(N'[eventdesk].[lucky_draw_rounds]') AND name=N'ed_lucky_draw_rounds_0') CREATE UNIQUE INDEX [ed_lucky_draw_rounds_0] ON [eventdesk].[lucky_draw_rounds] ([legacy_source]) WHERE [legacy_source] IS NOT NULL
GO

IF NOT EXISTS (SELECT 1 FROM sys.foreign_keys WHERE parent_object_id=OBJECT_ID(N'[eventdesk].[lucky_draw_rounds]') AND name=N'ed_fk_lucky_draw_rounds_0') ALTER TABLE [eventdesk].[lucky_draw_rounds] ADD CONSTRAINT [ed_fk_lucky_draw_rounds_0] FOREIGN KEY ([prize_id]) REFERENCES [eventdesk].[prizes] ([id])
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE object_id=OBJECT_ID(N'[eventdesk].[lucky_draw_winners]') AND name=N'ed_lucky_draw_winners_0') CREATE INDEX [ed_lucky_draw_winners_0] ON [eventdesk].[lucky_draw_winners] ([prize_id],[status])
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE object_id=OBJECT_ID(N'[eventdesk].[lucky_draw_winners]') AND name=N'ed_lucky_draw_winners_1') CREATE INDEX [ed_lucky_draw_winners_1] ON [eventdesk].[lucky_draw_winners] ([employee_id],[status])
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE object_id=OBJECT_ID(N'[eventdesk].[lucky_draw_winners]') AND name=N'ed_lucky_draw_winners_2') CREATE UNIQUE INDEX [ed_lucky_draw_winners_2] ON [eventdesk].[lucky_draw_winners] ([legacy_winner_id]) WHERE [legacy_winner_id] IS NOT NULL
GO

IF NOT EXISTS (SELECT 1 FROM sys.foreign_keys WHERE parent_object_id=OBJECT_ID(N'[eventdesk].[lucky_draw_winners]') AND name=N'ed_fk_lucky_draw_winners_0') ALTER TABLE [eventdesk].[lucky_draw_winners] ADD CONSTRAINT [ed_fk_lucky_draw_winners_0] FOREIGN KEY ([prize_id]) REFERENCES [eventdesk].[prizes] ([id])
GO

IF NOT EXISTS (SELECT 1 FROM sys.foreign_keys WHERE parent_object_id=OBJECT_ID(N'[eventdesk].[lucky_draw_winners]') AND name=N'ed_fk_lucky_draw_winners_1') ALTER TABLE [eventdesk].[lucky_draw_winners] ADD CONSTRAINT [ed_fk_lucky_draw_winners_1] FOREIGN KEY ([round_id]) REFERENCES [eventdesk].[lucky_draw_rounds] ([id])
GO

IF NOT EXISTS (SELECT 1 FROM sys.foreign_keys WHERE parent_object_id=OBJECT_ID(N'[eventdesk].[lucky_draw_winners]') AND name=N'ed_fk_lucky_draw_winners_2') ALTER TABLE [eventdesk].[lucky_draw_winners] ADD CONSTRAINT [ed_fk_lucky_draw_winners_2] FOREIGN KEY ([employee_id]) REFERENCES [eventdesk].[employees] ([id])
GO

IF NOT EXISTS (SELECT 1 FROM sys.foreign_keys WHERE parent_object_id=OBJECT_ID(N'[eventdesk].[lucky_draw_presentation]') AND name=N'ed_fk_lucky_draw_presentation_0') ALTER TABLE [eventdesk].[lucky_draw_presentation] ADD CONSTRAINT [ed_fk_lucky_draw_presentation_0] FOREIGN KEY ([winner_id]) REFERENCES [eventdesk].[lucky_draw_winners] ([id])
GO

IF OBJECT_ID(N'[eventdesk].[EventRevision]', N'U') IS NULL
        CREATE TABLE [eventdesk].[EventRevision] (singleton INT NOT NULL PRIMARY KEY CHECK(singleton=1), revision BIGINT NOT NULL);
        IF NOT EXISTS (SELECT 1 FROM [eventdesk].[EventRevision] WHERE singleton=1)
        INSERT INTO [eventdesk].[EventRevision] VALUES (1,0)
GO

IF OBJECT_ID(N'[eventdesk].[EventOperationReceipt]', N'U') IS NULL
        CREATE TABLE [eventdesk].[EventOperationReceipt] (
            operation_id VARCHAR(36) NOT NULL PRIMARY KEY, request_hash CHAR(64) NOT NULL,
            response_json NVARCHAR(MAX) NOT NULL, revision BIGINT NOT NULL,
            created_at DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME())
GO

IF OBJECT_ID(N'[eventdesk].[EventChangeJournal]', N'U') IS NULL
        CREATE TABLE [eventdesk].[EventChangeJournal] (
            revision BIGINT NOT NULL PRIMARY KEY, changes_json NVARCHAR(MAX) NOT NULL,
            created_at DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME())
GO

IF OBJECT_ID(N'[eventdesk].[EventMigrationReceipt]', N'U') IS NULL
        CREATE TABLE [eventdesk].[EventMigrationReceipt] (
            source_sha256 CHAR(64) NOT NULL PRIMARY KEY, manifest_json NVARCHAR(MAX) NOT NULL,
            applied_at DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME())
GO
