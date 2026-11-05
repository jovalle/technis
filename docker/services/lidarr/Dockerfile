FROM mcr.microsoft.com/dotnet/sdk:8.0.405@sha256:20767713e03a4a0d1bcd09d8a8606e8b6cfb815ddf96e1a0cf2cc059b57d55c0 AS build
WORKDIR /src
RUN curl -4fsSL --http1.1 --retry 3 --connect-timeout 15 --max-time 180 \
      https://codeload.github.com/Lidarr/Lidarr/tar.gz/refs/tags/v3.1.0.4875 -o /tmp/lidarr.tar.gz && \
    echo '91bd0a5db0db45ac8cdb8a3b74696cc6f37f0e31aaf24acd5d43aa41e466e87b  /tmp/lidarr.tar.gz' | sha256sum -c - && \
    tar -xzf /tmp/lidarr.tar.gz --strip-components=1
COPY rescan.patch /tmp/rescan.patch
# Additional album candidates already have database records outside the scan folder.
# Keep the release's dependency audit warnings visible while testing this assembly-only fix.
RUN git apply /tmp/rescan.patch && \
    dotnet test src/NzbDrone.Core.Test/Lidarr.Core.Test.csproj -c Release \
      --filter FullyQualifiedName~DiskScanServiceTests.ScanFixture \
      -p:EnableAnalyzers=false -p:AssemblyVersion=3.1.0.4875 \
      -p:SentryUploadSymbols=false -p:RuntimeIdentifiers=linux-x64 \
      -p:WarningsNotAsErrors=NU1902%3BNU1903 && \
    find /src/_output -name Lidarr.Core.dll -exec cp {} /Lidarr.Core.dll \; && \
    find /src/_output -name Lidarr.Core.pdb -exec cp {} /Lidarr.Core.pdb \;

FROM lscr.io/linuxserver/lidarr@sha256:d8549ff9ea29b4015c32257360652c1000c31ea471a2b3f0835ae77292faf35b
COPY --from=build /Lidarr.Core.dll /Lidarr.Core.pdb /app/lidarr/bin/
