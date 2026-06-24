package bambu_test

import (
	"bytes"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
)

// Golden tests run the built CLI over the sample × flag matrix and compare
// stdout byte-for-byte against fixtures captured from the original Python
// implementation. Profiles resolve against the vendored, hermetic fixture tree
// via BAMBU_PROFILE_DIR so results are reproducible without a local Bambu
// Studio install.

type goldenCase struct {
	label string
	args  []string
}

func matrix() []goldenCase {
	samples := []string{"overrides", "per-object", "saved-process", "storz", "test"}
	variants := []struct {
		name string
		args []string
	}{
		{"default", nil},
		{"friendly", []string{"--friendly-names"}},
		{"full", []string{"--full"}},
		{"full-friendly", []string{"--full", "--friendly-names"}},
		{"no-inheritance", []string{"--no-inheritance"}},
		{"select-mp", []string{"--select", "machine,process"}},
		{"select-f", []string{"--select", "filament"}},
		{"list", []string{"--list"}},
		{"obj0", []string{"--object", "0"}},
		{"obj0-friendly", []string{"--object", "0", "--friendly-names"}},
		{"obj0-full", []string{"--object", "0", "--full"}},
		{"obj0-select-p", []string{"--object", "0", "--select", "process"}},
	}
	var cases []goldenCase
	for _, s := range samples {
		sample := filepath.Join("..", "..", "samples", s+".3mf")
		for _, v := range variants {
			cases = append(cases, goldenCase{
				label: s + "__" + v.name,
				args:  append([]string{sample}, v.args...),
			})
		}
	}
	for _, s := range []string{"per-object", "saved-process", "storz"} {
		sample := filepath.Join("..", "..", "samples", s+".3mf")
		for _, n := range []string{"1", "2", "3", "4", "5"} {
			cases = append(cases,
				goldenCase{s + "__plate" + n, []string{sample, "--plate", n}},
				goldenCase{s + "__plate" + n + "-friendly", []string{sample, "--plate", n, "--friendly-names"}},
			)
		}
	}
	return cases
}

func TestGolden(t *testing.T) {
	// Build the CLI once into a temp dir (no artifact in the working tree).
	bin := filepath.Join(t.TempDir(), "print-bambu-3mf")
	build := exec.Command("go", "build", "-o", bin, "../../cmd/print-bambu-3mf")
	if out, err := build.CombinedOutput(); err != nil {
		t.Fatalf("build failed: %v\n%s", err, out)
	}

	profileDir, err := filepath.Abs(filepath.Join("..", "..", "testdata", "profiles"))
	if err != nil {
		t.Fatal(err)
	}
	goldenDir := filepath.Join("..", "..", "testdata", "golden")

	for _, tc := range matrix() {
		t.Run(tc.label, func(t *testing.T) {
			want, err := os.ReadFile(filepath.Join(goldenDir, tc.label+".out"))
			if err != nil {
				t.Fatalf("read golden: %v", err)
			}
			cmd := exec.Command(bin, tc.args...)
			cmd.Env = append(os.Environ(), "BAMBU_PROFILE_DIR="+profileDir)
			var stdout bytes.Buffer
			cmd.Stdout = &stdout
			if err := cmd.Run(); err != nil {
				t.Fatalf("run %v: %v", tc.args, err)
			}
			if !bytes.Equal(stdout.Bytes(), want) {
				t.Errorf("output mismatch for %s\n%s", tc.label, firstDiff(want, stdout.Bytes()))
			}
		})
	}
}

// firstDiff returns a short description of the first differing line.
func firstDiff(want, got []byte) string {
	wl := strings.Split(string(want), "\n")
	gl := strings.Split(string(got), "\n")
	for i := 0; i < len(wl) || i < len(gl); i++ {
		var w, g string
		if i < len(wl) {
			w = wl[i]
		}
		if i < len(gl) {
			g = gl[i]
		}
		if w != g {
			return "line " + itoa(i+1) + ":\n  want: " + w + "\n  got:  " + g
		}
	}
	return "(no line diff; trailing bytes differ)"
}

func itoa(n int) string {
	if n == 0 {
		return "0"
	}
	var b [20]byte
	i := len(b)
	for n > 0 {
		i--
		b[i] = byte('0' + n%10)
		n /= 10
	}
	return string(b[i:])
}
