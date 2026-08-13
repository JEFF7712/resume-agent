{
  description = "resume-agent: a closed-loop LaTeX resume harness for coding agents";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs =
    { self, nixpkgs }:
    let
      systems = [
        "x86_64-linux"
        "aarch64-linux"
        "x86_64-darwin"
        "aarch64-darwin"
      ];
      forAllSystems = f: nixpkgs.lib.genAttrs systems (system: f nixpkgs.legacyPackages.${system});

      texFor =
        pkgs:
        pkgs.texlive.withPackages (
          ps: with ps; [
            scheme-medium
            collection-latexextra
            collection-latexrecommended
            collection-fontsrecommended
            latexmk
            fontawesome5
            marvosym
            preprint
          ]
        );

      # pdftoppm/pdftotext/pdfinfo come from poppler; the fill check needs ImageMagick.
      runtimeFor = pkgs: [
        (texFor pkgs)
        pkgs.poppler-utils
        pkgs.imagemagick
        pkgs.python3
      ];

      compileFor =
        pkgs:
        pkgs.writeShellApplication {
          name = "compile";
          runtimeInputs = runtimeFor pkgs;
          text = ''
            set -euo pipefail
            root="$(pwd)"
            if [[ "''${1:-}" == "all" ]]; then
              mapfile -t found < <(find "$root" -maxdepth 1 -name '*resume.tex' -printf '%f\n' | sort)
              set -- "''${found[@]}"
            elif [[ "$#" -eq 0 ]]; then
              set -- example_resume.tex
            fi
            mkdir -p build
            rc=0
            for src in "$@"; do
              python3 "$root/hooks/resume_validate.py" "$src" || rc=1
              python3 "$root/hooks/resume_layout.py" "$src" || rc=1
            done
            exit "$rc"
          '';
        };
    in
    {
      devShells = forAllSystems (pkgs: {
        default = pkgs.mkShell {
          packages = runtimeFor pkgs ++ [
            pkgs.uv
            pkgs.ruff
            pkgs.pyright
          ];
        };
      });

      packages = forAllSystems (pkgs: {
        default = compileFor pkgs;
      });

      apps = forAllSystems (pkgs: {
        default = {
          type = "app";
          program = "${compileFor pkgs}/bin/compile";
        };
      });
    };
}
