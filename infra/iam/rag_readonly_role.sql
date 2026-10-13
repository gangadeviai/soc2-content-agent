-- Knowledge-base role used by the agent. SELECT only; verified at startup by app/rag.py.
CREATE ROLE kb_readonly LOGIN PASSWORD '<from-secrets-manager>';
GRANT CONNECT ON DATABASE kb TO kb_readonly;
GRANT USAGE ON SCHEMA public TO kb_readonly;
GRANT SELECT ON TABLE kb_chunks TO kb_readonly;
-- Row-level security as a second tenant barrier (app still filters explicitly).
ALTER TABLE kb_chunks ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_scope ON kb_chunks
  USING (tenant_id = current_setting('app.tenant_id', true));
ALTER ROLE kb_readonly SET default_transaction_read_only = on;
