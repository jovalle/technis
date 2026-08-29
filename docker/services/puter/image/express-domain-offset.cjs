"use strict";

const express = require("express");

const originalInit = express.application.init;

express.application.init = function initWithPublicDomainOffset() {
  originalInit.call(this);

  const publicDomain = process.env.PUTER_PUBLIC_DOMAIN?.trim();
  const offset = publicDomain?.split(".").filter(Boolean).length;

  if (Number.isSafeInteger(offset) && offset > 1) {
    this.set("subdomain offset", offset);
  }
};
