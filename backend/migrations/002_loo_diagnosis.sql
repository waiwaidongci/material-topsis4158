
-- Leave-one-out candidate diagnosis over an already completed, frozen run.
-- The referenced decision run is never rewritten; this table only stores
-- derived diagnosis state, checkpoints and results.

CREATE TABLE loo_diagnoses (
	id INTEGER NOT NULL AUTO_INCREMENT,
	run_id INTEGER NOT NULL,
	name VARCHAR(140) NOT NULL,
	status VARCHAR(20) NOT NULL,
	progress JSON NOT NULL,
	result JSON NOT NULL,
	summary JSON NOT NULL,
	error VARCHAR(600) NOT NULL,
	artifact_path VARCHAR(260) NOT NULL,
	created_at DATETIME NOT NULL,
	updated_at DATETIME NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (name),
	FOREIGN KEY(run_id) REFERENCES decision_runs (id)
)

;


-- One slim checkpoint row per excluded candidate. Full counterexample
-- digests are assembled into the artifact; the database keeps counts and
-- norm/ideal-point changes so progress and resume stay cheap.

CREATE TABLE loo_diagnosis_blocks (
	id INTEGER NOT NULL AUTO_INCREMENT,
	diagnosis_id INTEGER NOT NULL,
	excluded_code VARCHAR(40) NOT NULL,
	block JSON NOT NULL,
	created_at DATETIME NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (diagnosis_id, excluded_code),
	FOREIGN KEY(diagnosis_id) REFERENCES loo_diagnoses (id)
)

;
