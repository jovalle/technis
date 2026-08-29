DO $$
DECLARE
  all_users_group_id uuid;
BEGIN
  SELECT group_id
  INTO all_users_group_id
  FROM groups
  WHERE name = 'All Users'
    AND is_system = true;

  IF all_users_group_id IS NULL THEN
    RAISE EXCEPTION 'Kasm system group "All Users" was not found';
  END IF;

  UPDATE group_settings
  SET value = (
        COALESCE(NULLIF(value, '')::jsonb, '{}'::jsonb)
        || jsonb_build_object(
          'labels',
          COALESCE(NULLIF(value, '')::jsonb -> 'labels', '{}'::jsonb)
          || '{"dockhand.update":"false"}'::jsonb
        )
      )::text,
      value_type = 'json'
  WHERE group_id = all_users_group_id
    AND name = 'run_config';

  IF NOT FOUND THEN
    INSERT INTO group_settings (
      group_setting_id,
      name,
      value,
      value_type,
      description,
      group_id
    )
    VALUES (
      gen_random_uuid(),
      'run_config',
      '{"labels":{"dockhand.update":"false"}}',
      'json',
      'Specify arbitrary docker run params.',
      all_users_group_id
    );
  END IF;
END
$$;
