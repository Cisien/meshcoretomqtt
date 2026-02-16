{self, ...}: {
  imports = [
  ];
  perSystem = {
    pkgs,
    self',
    ...
  }: let
    version-info = pkgs.writeTextFile {
      name = "version-info";
      destination = "/.version_info";
      text = builtins.toJSON {
        installer_version = "Nix package build";
        git_hash = self.ref or "unknown";
        install_date = "unknown";
      };
    };
  in {
    # Package definitions
    packages.meshcore-decoder = pkgs.buildNpmPackage {
      name = "meshcore-decoder";
      version = "0.2.3";

      src = pkgs.fetchurl {
        url = "https://registry.npmjs.org/@michaelhart/meshcore-decoder/-/meshcore-decoder-0.2.3.tgz";
        sha256 = "sha256-iIT6tCORM+NtPfQBvBuoAUauPVIgI+L4lby7Umdptq8=";
      };

      npmDepsHash = "sha256-DMlLt4qlwQiBtSwHNM/zBFvN14p/DK+iaSvXBLIcw08=";

      # meshcore-decoder has no package lock. That makes Nix mad because it's not determinisitic
      postPatch = ''
        cp ${./package-lock.json} ./package-lock.json
      '';
      meta = {
        description = "A TypeScript library for decoding MeshCore mesh networking packets with full cryptographic support";
        homepage = "https://www.npmjs.com/package/@michaelhart/meshcore-decoder";
        license = pkgs.lib.licenses.mit;
      };
    };

    packages.default = pkgs.python313.pkgs.buildPythonPackage {
      name = "mctomqtt";
      src = ./..;
      format = "other"; # Since we have no setup.py/pyproject.toml

      propagatedBuildInputs = with pkgs.python313Packages; [
        paho-mqtt
        pyserial
      ];

      nativeBuildInputs = [
        pkgs.makeWrapper
        self'.packages.meshcore-decoder
      ];

      installPhase = ''
        # Install both Python files as modules
        mkdir -p $out/${pkgs.python313.sitePackages}
        install -Dm755 mctomqtt.py $out/${pkgs.python313.sitePackages}/mctomqtt.py
        install -Dm755 auth_token.py $out/${pkgs.python313.sitePackages}/auth_token.py
        # Copy the pre-generated version info file
        install -Dm644 ${version-info}/.version_info $out/${pkgs.python313.sitePackages}/.version_info


        # Create executable wrapper for mctomqtt
        mkdir -p $out/bin
        makeWrapper ${pkgs.python313.interpreter} $out/bin/mctomqtt \
          --add-flags "$out/${pkgs.python313.sitePackages}/mctomqtt.py" \
          --prefix PATH : ${pkgs.lib.makeBinPath [self'.packages.meshcore-decoder]} \
          --set PYTHONPATH "$out/${pkgs.python313.sitePackages}:${pkgs.python313.withPackages (ps: with ps; [paho-mqtt pyserial])}/${pkgs.python313.sitePackages}"
      '';

      meta = {
        description = "A Python-based script to send MeshCore debug and packet capture data to MQTT for analysis.";
        mainProgram = "meshcoretomqtt";
        license = pkgs.lib.licenses.mit;
        homepage = "https://github.com/Cisien/meshcoretomqtt";
      };
    };
  };
}
