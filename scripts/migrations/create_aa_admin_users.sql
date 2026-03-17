CREATE TABLE IF NOT EXISTS aa_admin_users (
    email TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
);

INSERT INTO aa_admin_users (email, name) VALUES
  ('noel.pinton@pivotwealth.com.au', 'Noel Pinton'),
  ('tim@pivotwealth.com.au', 'Tim Lea'),
  ('matthew.monceda@pivotwealth.com.au', 'Matthew Monceda')
ON CONFLICT (email) DO NOTHING;
