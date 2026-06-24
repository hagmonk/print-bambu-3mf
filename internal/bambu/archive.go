package bambu

import (
	"archive/zip"
	"io"
)

// archive wraps a 3MF (zip) file, reading entries by name on demand.
type archive struct {
	rc    *zip.ReadCloser
	files map[string]*zip.File
}

func openArchive(path string) (*archive, error) {
	rc, err := zip.OpenReader(path)
	if err != nil {
		return nil, err
	}
	files := make(map[string]*zip.File, len(rc.File))
	for _, f := range rc.File {
		files[f.Name] = f
	}
	return &archive{rc: rc, files: files}, nil
}

func (a *archive) Close() error { return a.rc.Close() }

func (a *archive) has(name string) bool {
	_, ok := a.files[name]
	return ok
}

func (a *archive) read(name string) ([]byte, error) {
	f, ok := a.files[name]
	if !ok {
		return nil, io.EOF
	}
	rc, err := f.Open()
	if err != nil {
		return nil, err
	}
	defer rc.Close()
	return io.ReadAll(rc)
}

// names returns entry names in archive (central-directory) order.
func (a *archive) names() []string {
	out := make([]string, 0, len(a.rc.File))
	for _, f := range a.rc.File {
		out = append(out, f.Name)
	}
	return out
}
